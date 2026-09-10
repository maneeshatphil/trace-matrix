import sys
import re
import psycopg2
from psycopg2.extras import execute_values
from pathlib import Path

# Ensure root directory is visible for imports
ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from src.config import DB_CONFIG, SOURCE_DOC_TYPES, TARGET_DOC_TYPES

# Thresholds for Pass 2 (Semantic Vector Match)
SEMANTIC_HIGH_THRESHOLD = 0.82      # Auto-approved match
SEMANTIC_SUGGESTED_THRESHOLD = 0.70  # Marked for human review in UI
SEMANTIC_TOP_K = 3                   # Candidates retrieved per source item

MIN_ID_LENGTH = 4                    # Ignore very short IDs, they match noise
_ALTERNATION_BATCH = 500             # Keep compiled regexes within reasonable size


def _compile_id_matchers(req_ids):
    """Compiles requirement IDs into alternation regexes, longest ID first so the
    most specific requirement wins at any given position."""
    ordered = sorted({r for r in req_ids if r and len(r) >= MIN_ID_LENGTH}, key=len, reverse=True)
    matchers = []
    for start in range(0, len(ordered), _ALTERNATION_BATCH):
        batch = ordered[start:start + _ALTERNATION_BATCH]
        alternation = "|".join(re.escape(r) for r in batch)
        # Custom boundaries so IDs embedded in names like "<ID>_ExpectedPatientId" still match
        matchers.append(re.compile(rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])"))
    return matchers


# --- PASS 1: EXACT REGEX MATCHING ---

def run_exact_regex_linker(cursor):
    """
    Pass 1: Matches explicit Requirement IDs appearing in Test Cases or Risk Items.
    """
    print("\n🔍 Running Pass 1: Deterministic Regex Matching...")

    # Fetch all Requirements (Targets)
    cursor.execute(
        "SELECT DISTINCT item_id, doc_type FROM document_chunks WHERE doc_type = ANY(%s);",
        (TARGET_DOC_TYPES,),
    )
    target_requirements = cursor.fetchall()

    if not target_requirements:
        print("⚠️ No Requirement chunks found in 'document_chunks'. Skipping Pass 1.")
        return set()

    req_doc_types = dict(target_requirements)
    matchers = _compile_id_matchers(req_doc_types.keys())

    # Fetch all Tests & Risk Items (Sources)
    cursor.execute(
        """
        SELECT item_id, doc_type, text_content, metadata
        FROM document_chunks
        WHERE doc_type = ANY(%s);
        """,
        (SOURCE_DOC_TYPES,),
    )
    sources = cursor.fetchall()

    matched_sources = set()
    links = {}

    for source_id, source_doc_type, text_content, metadata in sources:
        # Search text content and metadata for explicit target IDs
        searchable_text = f"{source_id} {text_content} {metadata}"

        for matcher in matchers:
            for match in matcher.finditer(searchable_text):
                req_id = match.group(0)
                if req_id == source_id:
                    continue  # Never link an item to itself
                matched_sources.add(source_id)
                links[(source_id, req_id)] = (
                    source_id, source_doc_type,
                    req_id, req_doc_types[req_id],
                    'EXACT_REGEX', 1.0, 'ACTIVE'
                )

    if links:
        save_links_to_db(cursor, list(links.values()))
        print(f"✅ Pass 1 Complete: Established {len(links)} exact link(s).")
    else:
        print("ℹ️ Pass 1 Complete: No exact regex matches found.")

    return matched_sources


# --- PASS 2: SEMANTIC PGVECTOR SEARCH ---

def run_semantic_vector_linker(cursor, matched_sources):
    """
    Pass 2: Computes cosine similarity via pgvector for items unlinked in Pass 1.
    """
    print("\n🧠 Running Pass 2: Semantic pgvector Similarity Search...")

    # Fetch sources (TEST/RMM) that were NOT matched in Pass 1
    cursor.execute(
        """
        SELECT id, item_id, doc_type
        FROM document_chunks
        WHERE doc_type = ANY(%s) AND embedding IS NOT NULL;
        """,
        (SOURCE_DOC_TYPES,),
    )
    all_sources = cursor.fetchall()
    unlinked_sources = [s for s in all_sources if s[1] not in matched_sources]

    if not unlinked_sources:
        print("ℹ️ All source items were linked in Pass 1. Skipping Pass 2.")
        return

    print(f"⚙️ Evaluating {len(unlinked_sources)} unlinked source item(s) against Requirements...")

    # ORDER BY distance without a distance predicate lets pgvector use the HNSW index
    query = """
        WITH src AS (SELECT embedding FROM document_chunks WHERE id = %s)
        SELECT
            req.item_id,
            req.doc_type,
            1 - (req.embedding <=> (SELECT embedding FROM src)) AS similarity_score
        FROM document_chunks req
        WHERE req.doc_type = ANY(%s)
          AND req.id <> %s
          AND req.embedding IS NOT NULL
        ORDER BY req.embedding <=> (SELECT embedding FROM src)
        LIMIT %s;
    """

    semantic_links = {}

    for chunk_db_id, source_id, source_doc_type in unlinked_sources:
        cursor.execute(query, (chunk_db_id, TARGET_DOC_TYPES, chunk_db_id, SEMANTIC_TOP_K))

        for target_id, target_doc_type, score in cursor.fetchall():
            score = float(score)
            if score < SEMANTIC_SUGGESTED_THRESHOLD or target_id == source_id:
                continue

            if score >= SEMANTIC_HIGH_THRESHOLD:
                match_type = 'SEMANTIC_HIGH'
                status = 'ACTIVE'
            else:
                match_type = 'SEMANTIC_SUGGESTED'
                status = 'NEEDS_REVIEW'

            key = (source_id, target_id)
            # Keep the strongest score when the same pair is retrieved twice
            if key not in semantic_links or semantic_links[key][5] < score:
                semantic_links[key] = (
                    source_id, source_doc_type,
                    target_id, target_doc_type,
                    match_type, round(score, 4), status
                )

    if semantic_links:
        save_links_to_db(cursor, list(semantic_links.values()))
        print(f"✅ Pass 2 Complete: Established {len(semantic_links)} semantic link(s).")
    else:
        print("ℹ️ Pass 2 Complete: No items met the semantic similarity threshold.")


# --- DATABASE INSERT HELPER ---

def save_links_to_db(cursor, links):
    """Inserts generated traceability links into the database using upsert logic.
    Links must be deduplicated by (source, target) or the upsert affects a row twice."""
    insert_query = """
        INSERT INTO traceability_matrix 
        (source_item_id, source_doc_type, target_item_id, target_doc_type, match_type, confidence_score, status)
        VALUES %s
        ON CONFLICT (source_item_id, target_item_id)
        DO UPDATE SET 
            match_type = EXCLUDED.match_type,
            confidence_score = EXCLUDED.confidence_score,
            status = EXCLUDED.status,
            created_at = CURRENT_TIMESTAMP;
    """
    execute_values(cursor, insert_query, links, page_size=200)


# --- MAIN ENGINE ENTRYPOINT ---

def run_engine_3():
    """Executes Engine 3 Pipeline."""
    print("🚀 Starting Engine 3 (The Linker)...")
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()

        # Step 1: Run Exact Regex Matching
        matched_sources = run_exact_regex_linker(cursor)

        # Step 2: Run Semantic Vector Search for remaining unlinked items
        run_semantic_vector_linker(cursor, matched_sources)

        cursor.execute(
            "UPDATE parsed_documents SET status = 'LINKED' WHERE status = 'READY_FOR_LINKING';"
        )

        conn.commit()
        cursor.close()
        print("\n🎉 Engine 3 Execution Completed Successfully!")

    except Exception as e:
        if conn is not None:
            conn.rollback()
        print(f"❌ [ENGINE 3 ERROR] Pipeline execution failed: {e}")
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    run_engine_3()