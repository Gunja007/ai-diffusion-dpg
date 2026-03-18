"""
action_gateway/main.py

Entry point for the Action Gateway mock ONEST server.

Starts the FastAPI server on port 9999. Run from the action_gateway/ directory:
    python main.py

Or from repo root:
    python -m action_gateway.main
"""

import logging
import uvicorn

from src.mock_server import app  # noqa: F401 — uvicorn imports the app object

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

if __name__ == "__main__":
    uvicorn.run(
        "src.mock_server:app",
        host="0.0.0.0",
        port=9999,
        reload=False,
        log_level="info",
    )
