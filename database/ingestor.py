import os
import sys
import psycopg2
from psycopg2.extras import Json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from src.config import DB_CONFIG

def ingest_to_postgres(output_json_path: str, ingestion_output_data: dict) -> bool:
    """
    Called automatically by Engine 1 (Parser) right after parsing.
    Inserts JSON payload into PostgreSQL with status = 'PENDING_LINKING'
    so Engine 2 (Chunking) can pick it up autonomously.
    """
    file_name = os.path.basename(output_json_path)
    document_id = ingestion_output_data.get("document_id", "UNKNOWN")
    source_type = ingestion_output_data.get("source_type", "UNKNOWN")
    # doc_type must be a logical class (PRS/PTD/TEST/RMM) because Engine 3 filters on it
    doc_type = ingestion_output_data.get("doc_class") or "UNKNOWN"

    insert_query = """
        INSERT INTO parsed_documents (file_name, doc_type, source_type, raw_payload, status)
        VALUES (%s, %s, %s, %s, 'PENDING_LINKING')
        ON CONFLICT (file_name)
        DO UPDATE SET
            doc_type = EXCLUDED.doc_type,
            source_type = EXCLUDED.source_type,
            raw_payload = EXCLUDED.raw_payload,
            status = 'PENDING_LINKING',
            created_at = CURRENT_TIMESTAMP;
    """

    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    insert_query,
                    (file_name, doc_type, source_type, Json(ingestion_output_data)),
                )

        print(f"🚀 [ENGINE 1 -> DB] Saved '{file_name}' as {doc_type} (Doc ID: {document_id}).")
        return True

    except Exception as e:
        print(f"❌ [ENGINE 1 DB ERROR] Failed to store '{file_name}': {e}")
        return False