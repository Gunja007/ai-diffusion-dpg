"""
knowledge_engine/scripts/ingest.py

Offline document ingestion script.

Loads documents from sources defined in config/config.yaml, chunks them,
embeds them, and stores them in the ChromaDB vector store.

Run once before first deployment and whenever source documents change:

    # From the knowledge_engine/ directory:
    python -m scripts.ingest
    python -m scripts.ingest --config config/config.yaml

This script is NOT part of the runtime response path. It runs fully offline
and must be completed before the Knowledge Engine service starts.
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path.resolve()}")
    with config_path.open("r") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(config_path: str = "config/config.yaml") -> None:
    """
    Run the full ingestion pipeline.

    1. Load YAML config.
    2. Instantiate StaticKnowledgeBaseBlock.
    3. Call block.ingest(config) which:
       - Creates or recreates the ChromaDB collection.
       - Loads each source document (PDF, CSV, Markdown).
       - Chunks and embeds documents.
       - Stores vectors + metadata in ChromaDB.
    """
    load_dotenv()

    logger.info(
        "ingest.start",
        extra={"operation": "ingest.main", "config_path": config_path},
    )

    config = _load_config(config_path)

    # Import here to avoid circular imports at module level
    from src.blocks.static_knowledge_base import StaticKnowledgeBaseBlock

    block = StaticKnowledgeBaseBlock()

    try:
        block.ingest(config)
        logger.info(
            "ingest.complete",
            extra={"operation": "ingest.main", "status": "success"},
        )
    except Exception as e:
        logger.error(
            "ingest.failed",
            extra={
                "operation": "ingest.main",
                "status": "failure",
                "error": f"{type(e).__name__}: {e}",
            },
        )
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest knowledge documents into ChromaDB for the Knowledge Engine."
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config YAML file (default: config/config.yaml)",
    )
    args = parser.parse_args()
    main(config_path=args.config)
