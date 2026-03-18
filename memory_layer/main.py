"""
memory_layer/main.py

Entry point for the Memory Layer FastAPI service.

Loads config from config/config.yaml, instantiates InProcessSessionMemory,
creates the FastAPI app, and starts uvicorn on the configured port (default 8002).

Run:
    python -m main                   (from memory_layer/ directory)
    uvicorn main:app --reload        (dev hot-reload)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import uvicorn
import yaml

# Add src/ to path so imports within the package work cleanly.
_SRC = str(Path(__file__).parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from session_memory import InProcessSessionMemory
from server import create_app

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


def _load_config(path: str = "config/config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path.resolve()}")
    with config_path.open("r") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# App construction — exposed at module level for uvicorn --reload
# ---------------------------------------------------------------------------


def _build_app():
    config = _load_config()

    memory = InProcessSessionMemory(config)
    app = create_app(memory)

    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8002)

    logger.info(
        "memory_layer.startup",
        extra={
            "operation": "main.startup",
            "status": "success",
            "host": host,
            "port": port,
        },
    )

    return app, host, port


app, _host, _port = _build_app()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=_host,
        port=_port,
        log_level="info",
    )
