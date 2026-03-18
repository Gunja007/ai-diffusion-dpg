"""
knowledge_engine/main.py

Startup entrypoint for the Knowledge Engine service.

Responsibilities:
- Load config from config/config.yaml
- Instantiate KnowledgeEngine (no LLM proxy needed — Language Norm and NLU
  now run in Agent Core; KE runs only Glossary, Static KB, Multimodal)
- Expose FastAPI endpoints: POST /assemble_prompt, GET /health
- Start uvicorn HTTP server on configured host:port (default 8001)

Run:
    python main.py                       (from knowledge_engine/ directory)
    uvicorn main:app --reload            (dev hot-reload)

Environment:
    OPENAI_API_KEY — required only if embedding_provider=openai in config.yaml.
"""

import logging
import sys
from pathlib import Path
from typing import Any, Optional

import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Load .env before anything reads environment variables.
# Has no effect if .env does not exist (safe in production).
load_dotenv()

from src.engine import KnowledgeEngine
from src.models import SessionState

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
# Pydantic schemas for HTTP API
# ---------------------------------------------------------------------------


class SessionStateSchema(BaseModel):
    """Serialised form of SessionState for HTTP transport."""
    session_id: str
    history: list[dict] = []
    confirmed_entities: dict[str, Any] = {}
    workflow_step: Optional[str] = None
    user_profile: dict[str, Any] = {}


class AssemblePromptRequest(BaseModel):
    session_id: str
    user_message: str
    session_state: SessionStateSchema
    # NLU results pre-computed by Agent Core (steps 3-4 of process_turn)
    normalised_input: str = ""
    detected_language: str = ""
    intent: str = "unknown"
    entities: dict[str, Any] = {}
    sentiment: str = "neutral"
    confidence: float = 0.0


class AssemblePromptResponse(BaseModel):
    messages: list[dict]
    session_id: str


class HealthResponse(BaseModel):
    status: str


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
# App factory
# ---------------------------------------------------------------------------


def create_app(ke: KnowledgeEngine, config: dict) -> FastAPI:
    """
    Create and return the FastAPI application with KnowledgeEngine bound to it.

    Args:
        ke:     Instantiated KnowledgeEngine.
        config: Full config dict (used for health endpoint metadata).
    """
    if ke is None:
        raise ValueError("ke must not be None")

    app = FastAPI(
        title="Knowledge Engine DPG",
        description="NLU, RAG, and prompt assembly for the AI Composition Framework.",
        version="0.1.0",
        docs_url="/docs",
    )
    app.state.ke = ke
    app.state.config = config

    # ----------------------------------------------------------------
    # POST /assemble_prompt
    # ----------------------------------------------------------------

    @app.post("/assemble_prompt", response_model=AssemblePromptResponse)
    def assemble_prompt(request: AssemblePromptRequest) -> AssemblePromptResponse:
        """
        Assemble the complete LLM prompt for one conversation turn.

        Accepts the session state from Agent Core, runs all enabled blocks,
        and returns the fully assembled messages list.

        Returns HTTP 422 if session_id or user_message is missing.
        Always returns AssemblePromptResponse — never returns HTTP 5xx for
        block-level failures (those are absorbed internally).
        """
        if not request.session_id:
            raise HTTPException(status_code=422, detail="session_id must not be empty")

        # Deserialise session state
        session_state = SessionState(
            session_id=request.session_state.session_id,
            history=request.session_state.history,
            confirmed_entities=request.session_state.confirmed_entities,
            workflow_step=request.session_state.workflow_step,
            user_profile=request.session_state.user_profile,
        )

        messages = app.state.ke.assemble_prompt(
            session_id=request.session_id,
            user_message=request.user_message,
            session_state=session_state,
            normalised_input=request.normalised_input,
            detected_language=request.detected_language,
            intent=request.intent,
            entities=request.entities,
            sentiment=request.sentiment,
            confidence=request.confidence,
        )

        logger.info(
            "ke_server.assemble_prompt",
            extra={
                "operation": "ke_server.assemble_prompt",
                "status": "success",
                "session_id": request.session_id,
                "message_count": len(messages),
            },
        )

        return AssemblePromptResponse(
            messages=messages,
            session_id=request.session_id,
        )

    # ----------------------------------------------------------------
    # GET /health
    # ----------------------------------------------------------------

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Readiness probe. Confirms KE service is up."""
        return HealthResponse(status="ok")

    return app


# ---------------------------------------------------------------------------
# App construction — exposed at module level for uvicorn --reload
# ---------------------------------------------------------------------------


def _build_app():
    config = _load_config()

    knowledge_cfg = config.get("knowledge", {})
    if not knowledge_cfg:
        raise ValueError("Config missing required 'knowledge' section")

    # KE no longer needs an LLM proxy — Language Normalisation and NLU run in Agent Core.
    # llm=None is fine while multimodal block is disabled; pass an LLMWrapperBase
    # implementation here when multimodal is enabled.
    ke = KnowledgeEngine(config=config)

    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8001)

    app = create_app(ke, config)

    logger.info(
        "knowledge_engine.startup",
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
