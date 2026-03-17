"""
agent_core/main.py

Startup entrypoint for the Agent Core service.

Responsibilities:
- Load config from config/config.yaml
- Instantiate ClaudeLLMWrapper with agent config
- Create the FastAPI app via create_app()
- Start the uvicorn HTTP server on configured host:port

Run:
    python -m main                    (from agent_core/ directory)
    uvicorn main:app --reload         (dev hot-reload)

Environment:
    ANTHROPIC_API_KEY must be set. ClaudeLLMWrapper reads it from the environment
    via the Anthropic SDK — never hardcoded here.
"""

import logging
import sys
from pathlib import Path

import uvicorn
import yaml
from dotenv import load_dotenv

# Load .env before anything reads ANTHROPIC_API_KEY from the environment.
# Has no effect if .env does not exist (safe in production where the var is
# injected by the orchestrator / secrets manager directly).
load_dotenv()

from src.llm_wrapper.claude_wrapper import ClaudeLLMWrapper
from src.llm_proxy_server import create_app

# ---------------------------------------------------------------------------
# Logging — structured output, INFO level default
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
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# App construction — exposed at module level for uvicorn --reload
# ---------------------------------------------------------------------------

def _build_app():
    config = _load_config()

    agent_cfg = config.get("agent")
    if not agent_cfg:
        raise ValueError("Config missing required 'agent' section")

    llm = ClaudeLLMWrapper(agent_cfg)

    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8000)

    app = create_app(llm)

    logger.info(
        "agent_core.startup",
        extra={
            "operation": "main.startup",
            "status": "success",
            "host": host,
            "port": port,
            "model": llm.get_active_model(),
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
