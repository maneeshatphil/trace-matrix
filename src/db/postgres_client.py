"""Read/write access layer used by the Streamlit application."""

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

import pandas as pd
import psycopg2

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from src.config import DB_CONFIG, SOURCE_DOC_TYPES, TARGET_DOC_TYPES


@contextmanager
def get_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


def _read_sql(query: str, params: Optional[tuple] = None) -> pd.DataFrame:
    """Builds a DataFrame straight from the cursor; pandas warns when handed a raw
    psycopg2 connection, and SQLAlchemy is not a dependency of this project."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
    return pd.DataFrame(rows, columns=columns)


def check_health() -> tuple:
    """Returns (is_reachable, message) so the UI can fail gracefully."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        return True, "Connected"
    except Exception as exc:
        return False, str(exc).strip()


def get_documents() -> pd.DataFrame:
    return _read_sql(
        """
        SELECT id, file_name, doc_type, source_type, status, created_at
        FROM parsed_documents
        ORDER BY created_at DESC;
        """
    )


def get_summary() -> dict:
    """Headline coverage numbers for the dashboard KPI row."""
    reqs = _read_sql(
        "SELECT COUNT(DISTINCT item_id) AS n FROM document_chunks WHERE doc_type = ANY(%s);",
        (TARGET_DOC_TYPES,),
    )["n"].iloc[0]

    sources = _read_sql(
        "SELECT COUNT(DISTINCT item_id) AS n FROM document_chunks WHERE doc_type = ANY(%s);",
        (SOURCE_DOC_TYPES,),
    )["n"].iloc[0]

    covered = _read_sql(
        """
        SELECT COUNT(DISTINCT target_item_id) AS n
        FROM traceability_matrix
        WHERE status <> 'REJECTED';
        """
    )["n"].iloc[0]

    linked_sources = _read_sql(
        """
        SELECT COUNT(DISTINCT source_item_id) AS n
        FROM traceability_matrix
        WHERE status <> 'REJECTED';
        """
    )["n"].iloc[0]

    totals = _read_sql(
        """
        SELECT
            COUNT(*) AS total_links,
            COUNT(*) FILTER (WHERE status = 'NEEDS_REVIEW') AS pending_review
        FROM traceability_matrix;
        """
    )

    return {
        "requirements": int(reqs),
        "requirements_covered": int(covered),
        "sources": int(sources),
        "sources_linked": int(linked_sources),
        "total_links": int(totals["total_links"].iloc[0]),
        "pending_review": int(totals["pending_review"].iloc[0]),
    }


def get_links(
    match_types: Optional[List[str]] = None,
    statuses: Optional[List[str]] = None,
    search: Optional[str] = None,
    min_confidence: float = 0.0,
) -> pd.DataFrame:
    """Traceability links enriched with the text of both endpoints.

    DISTINCT ON guards against a chunk id appearing more than once per item_id,
    which would otherwise fan the matrix out with duplicate rows.
    """
    query = """
        WITH chunk_text AS (
            SELECT DISTINCT ON (item_id)
                   item_id, text_content, metadata->>'title' AS title
            FROM document_chunks
            ORDER BY item_id, id
        )
        SELECT
            tm.id,
            tm.source_item_id,
            tm.source_doc_type,
            tm.target_item_id,
            tm.target_doc_type,
            tm.match_type,
            tm.confidence_score,
            tm.status,
            LEFT(COALESCE(s.text_content, ''), 300) AS source_text,
            LEFT(COALESCE(t.text_content, ''), 300) AS target_text
        FROM traceability_matrix tm
        LEFT JOIN chunk_text s ON s.item_id = tm.source_item_id
        LEFT JOIN chunk_text t ON t.item_id = tm.target_item_id
        WHERE tm.confidence_score >= %s
    """
    params: list = [min_confidence]

    if match_types:
        query += " AND tm.match_type = ANY(%s)"
        params.append(match_types)
    if statuses:
        query += " AND tm.status = ANY(%s)"
        params.append(statuses)
    if search:
        query += " AND (tm.source_item_id ILIKE %s OR tm.target_item_id ILIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])

    query += " ORDER BY tm.confidence_score DESC, tm.source_item_id;"
    return _read_sql(query, tuple(params))


def get_uncovered_requirements() -> pd.DataFrame:
    """Requirements with no surviving link - the compliance gap that matters most."""
    return _read_sql(
        """
        SELECT DISTINCT ON (c.item_id)
               c.item_id, c.doc_type, c.parent_doc_id,
               LEFT(c.text_content, 300) AS text_content
        FROM document_chunks c
        WHERE c.doc_type = ANY(%s)
          AND NOT EXISTS (
              SELECT 1 FROM traceability_matrix tm
              WHERE tm.target_item_id = c.item_id AND tm.status <> 'REJECTED'
          )
        ORDER BY c.item_id, c.id;
        """,
        (TARGET_DOC_TYPES,),
    )


def get_unlinked_sources(doc_type: Optional[str] = None) -> pd.DataFrame:
    """Tests/risks that trace to nothing, so their evidence is unusable."""
    types = [doc_type] if doc_type else SOURCE_DOC_TYPES
    return _read_sql(
        """
        SELECT DISTINCT ON (c.item_id)
               c.item_id, c.doc_type, c.item_type, c.parent_doc_id,
               LEFT(c.text_content, 300) AS text_content
        FROM document_chunks c
        WHERE c.doc_type = ANY(%s)
          AND NOT EXISTS (
              SELECT 1 FROM traceability_matrix tm
              WHERE tm.source_item_id = c.item_id AND tm.status <> 'REJECTED'
          )
        ORDER BY c.item_id, c.id;
        """,
        (types,),
    )


def get_coverage_by_document() -> pd.DataFrame:
    return _read_sql(
        """
        WITH reqs AS (
            SELECT DISTINCT parent_doc_id, item_id
            FROM document_chunks
            WHERE doc_type = ANY(%s)
        )
        SELECT
            r.parent_doc_id AS document,
            COUNT(*) AS requirements,
            COUNT(*) FILTER (
                WHERE EXISTS (
                    SELECT 1 FROM traceability_matrix tm
                    WHERE tm.target_item_id = r.item_id AND tm.status <> 'REJECTED'
                )
            ) AS covered
        FROM reqs r
        GROUP BY r.parent_doc_id
        ORDER BY 1;
        """,
        (TARGET_DOC_TYPES,),
    )


def get_match_type_breakdown() -> pd.DataFrame:
    return _read_sql(
        """
        SELECT match_type, status, COUNT(*) AS links,
               ROUND(AVG(confidence_score)::numeric, 3) AS avg_confidence
        FROM traceability_matrix
        GROUP BY match_type, status
        ORDER BY links DESC;
        """
    )


def get_item_detail(item_id: str) -> pd.DataFrame:
    return _read_sql(
        """
        SELECT item_id, parent_doc_id, doc_type, item_type, text_content, metadata
        FROM document_chunks
        WHERE item_id = %s
        ORDER BY id
        LIMIT 1;
        """,
        (item_id,),
    )


def update_link_status(link_ids: List[int], status: str) -> int:
    """Applies a reviewer decision to one or more suggested links."""
    if not link_ids:
        return 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE traceability_matrix SET status = %s WHERE id = ANY(%s);",
                (status, list(link_ids)),
            )
            affected = cur.rowcount
        conn.commit()
    return affected
