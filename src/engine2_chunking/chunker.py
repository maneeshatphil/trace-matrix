import sys
import json
import psycopg2
from psycopg2.extras import Json, execute_values
from pathlib import Path

# Ensure root directory is in python path for imports
ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from src.config import DB_CONFIG, EMBEDDING_MODEL, EMBEDDING_DIM

_embedder = None


def get_embedder():
    """Loads the sentence-transformer on first use; importing this module must stay cheap
    because pulling in sentence-transformers drags the whole torch stack with it."""
    global _embedder
    if _embedder is not None:
        return _embedder

    from sentence_transformers import SentenceTransformer

    print(f"⏳ Loading embedding model '{EMBEDDING_MODEL}'...")
    model = SentenceTransformer(EMBEDDING_MODEL)
    model_dim = model.get_sentence_embedding_dimension()
    if model_dim != EMBEDDING_DIM:
        raise RuntimeError(
            f"Embedding dimension mismatch: '{EMBEDDING_MODEL}' produces {model_dim}-d vectors but the "
            f"schema expects {EMBEDDING_DIM}. Update database/db.txt and RTM_EMBEDDING_DIM."
        )
    print(f"✅ Embedding model loaded ({model_dim} dimensions).")
    _embedder = model
    return _embedder


def fetch_pending_documents(cursor):
    """Retrieves all documents waiting to be chunked and vectorized."""
    query = """
        SELECT id, file_name, doc_type, raw_payload 
        FROM parsed_documents 
        WHERE status = 'PENDING_LINKING';
    """
    cursor.execute(query)
    return cursor.fetchall()


def process_and_store_chunks(conn, cursor, doc_record):
    """Parses raw_payload items, generates embeddings, and inserts into document_chunks."""
    doc_id_db, file_name, doc_type, raw_payload = doc_record

    # Ensure raw_payload is loaded as dict
    payload = raw_payload if isinstance(raw_payload, dict) else json.loads(raw_payload)
    items = payload.get("items", [])
    parent_doc_id = payload.get("document_id", "UNKNOWN")

    if not items:
        print(f"⚠️ No items found in payload for file '{file_name}'. Marking as READY_FOR_LINKING.")
        update_doc_status(cursor, doc_id_db, 'READY_FOR_LINKING')
        conn.commit()
        return

    print(f"⚙️ Processing {len(items)} items from '{file_name}' (Doc ID: {parent_doc_id})...")

    # Re-ingestion is idempotent: drop previously generated chunks for this document
    cursor.execute("DELETE FROM document_chunks WHERE parent_doc_id = %s;", (parent_doc_id,))

    # Collect texts for batch embedding (faster performance)
    texts_to_embed = [item.get("text_content", "") or "" for item in items]

    # Normalized vectors keep cosine distance well-behaved in pgvector
    embeddings = get_embedder().encode(
        texts_to_embed,
        batch_size=32,
        normalize_embeddings=True,
        show_progress_bar=False,
    ).tolist()

    insert_chunk_query = """
        INSERT INTO document_chunks 
        (parent_doc_id, item_id, doc_type, item_type, text_content, metadata, embedding)
        VALUES %s;
    """

    rows = []
    for item, text_content, embedding_vector in zip(items, texts_to_embed, embeddings):
        item_id = item.get("item_id", "UNKNOWN_ITEM")
        item_type = item.get("item_type", "UNKNOWN_TYPE")

        # Combine visual evidence, missing artifacts, and metadata
        metadata = {
            "source_filename": file_name,
            "section_path": item.get("section_path", ""),
            "title": item.get("title"),
            "metadata": item.get("metadata", {}),
            "visual_evidence": item.get("visual_evidence", []),
            "missing_artifacts": item.get("missing_artifacts", [])
        }

        rows.append((
            parent_doc_id, item_id, doc_type, item_type,
            text_content, Json(metadata), embedding_vector
        ))

    execute_values(cursor, insert_chunk_query, rows, page_size=200)

    # Update state in parsed_documents to prevent reprocessing
    update_doc_status(cursor, doc_id_db, 'READY_FOR_LINKING')
    conn.commit()
    print(f"✅ Chunked and vectorized {len(rows)} item(s) from '{file_name}'. Status: 'READY_FOR_LINKING'.\n")


def update_doc_status(cursor, doc_id, status_string):
    """Updates the ingestion status of a parsed document record."""
    cursor.execute(
        "UPDATE parsed_documents SET status = %s WHERE id = %s;",
        (status_string, doc_id)
    )


def run_engine_2(raise_on_error: bool = False):
    """Main execution entrypoint for Engine 2. Returns (documents_chunked, failure_messages).
    With raise_on_error the caller (the orchestrator) gets the exception instead of a log line."""
    conn = None
    processed = 0
    failures = []
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()

        pending_docs = fetch_pending_documents(cursor)
        if not pending_docs:
            print("ℹ️ Engine 2: No 'PENDING_LINKING' documents found. Everything is up to date.")
            return processed, failures

        print(f"🔍 Engine 2 Found {len(pending_docs)} document(s) pending processing.")

        for doc in pending_docs:
            try:
                process_and_store_chunks(conn, cursor, doc)
                processed += 1
            except Exception as doc_error:
                # One bad document must not abort the batch, nor be retried on every future run
                conn.rollback()
                failures.append(f"{doc[1]}: {doc_error}")
                print(f"❌ [ENGINE 2 ERROR] Failed to process '{doc[1]}': {doc_error}")
                update_doc_status(cursor, doc[0], 'FAILED')
                conn.commit()

        cursor.close()

        if failures and processed == 0 and raise_on_error:
            raise RuntimeError("Engine 2 could not chunk any document: " + "; ".join(failures))

    except Exception as e:
        if raise_on_error:
            raise
        print(f"❌ [ENGINE 2 ERROR] Pipeline failed: {e}")
    finally:
        if conn is not None:
            conn.close()

    return processed, failures


if __name__ == "__main__":
    run_engine_2()