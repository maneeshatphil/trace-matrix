import os

DB_CONFIG = {
    "dbname": os.getenv("RTM_DB_NAME", "rtm_db"),
    "user": os.getenv("RTM_DB_USER", "admin"),
    "password": os.getenv("RTM_DB_PASSWORD", "password123"),
    "host": os.getenv("RTM_DB_HOST", "localhost"),
    # Host port published by docker-compose (container listens on 5432 internally)
    "port": os.getenv("RTM_DB_PORT", "5433"),
}

EMBEDDING_MODEL = os.getenv("RTM_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
# Must match the vector(N) dimension declared in database/db.txt
EMBEDDING_DIM = int(os.getenv("RTM_EMBEDDING_DIM", "384"))

# Logical document classes used by the linker
DOC_CLASS_PRS = "PRS"   # Product/user requirements
DOC_CLASS_PTD = "PTD"   # Product technical design
DOC_CLASS_TEST = "TEST"  # Test evidence
DOC_CLASS_RMM = "RMM"   # Risk management / FMEA

TARGET_DOC_TYPES = [DOC_CLASS_PRS, DOC_CLASS_PTD]
SOURCE_DOC_TYPES = [DOC_CLASS_TEST, DOC_CLASS_RMM]
