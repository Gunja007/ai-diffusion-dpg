"""
action_gateway/main.py

Entry point for the Action Gateway mock ONEST server.

Starts the FastAPI server on the host/port read from config/dpg.yaml + config/domain.yaml
(default: 0.0.0.0:9999). DPG config missing → hard failure. Domain config missing → service
runs with DPG defaults; exceptions thrown at request time when domain values are accessed.

Run from the action_gateway/ directory:
    python main.py

Or from repo root:
    python -m action_gateway.main
"""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
import yaml

from src.mock_server import app  # noqa: F401 — uvicorn imports the app object

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


def _load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path.resolve()}")
    with config_path.open("r") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base. Override values win. Dicts are merged recursively."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _build_config() -> tuple[dict, str, int]:
    dpg_config = _load_config("config/dpg.yaml")
    domain_config = _load_config("config/domain.yaml")
    config = _deep_merge(dpg_config, domain_config)
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 9999)
    return config, host, port


if __name__ == "__main__":
    config, host, port = _build_config()

    logger.info(
        "action_gateway.startup",
        extra={"operation": "main", "status": "success", "host": host, "port": port},
    )

    uvicorn.run(
        "src.mock_server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
