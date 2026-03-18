"""
reach_layer/main.py — KKB PoC CLI Reach Layer

Thin terminal client that talks to the Agent Core orchestration service.
This is the Reach Layer for CLI channel — the entry point for developer testing.

In production, the Reach Layer is replaced by a channel adapter (WhatsApp, Web, VOIP).
This CLI variant reads from stdin and POSTs to Agent Core via HTTP.

Prerequisites (run each in a separate terminal before this):
    Terminal 1: cd memory_layer   && python main.py   (port 8002)
    Terminal 2: cd trust_layer    && python main.py   (port 8003)
    Terminal 3: cd learning_layer && python main.py   (port 8004)
    Terminal 4: cd knowledge_engine && python main.py (port 8001)
    Terminal 5: cd action_gateway && python main.py   (port 9999)
    Terminal 6: cd agent_core     && python main.py   (port 8000)

Then run from reach_layer/:
    python main.py

KKB demo scenario (profile: electrician, Hubli, Hindi):
    "mujhe kaam chahiye" → market_truth_query → ONEST lookup → salary range
    "PMKVY ke baare mein batao" → scheme_query → schemes RAG context
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

import httpx
import yaml

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
# Config: read Agent Core endpoint from reach_layer's own config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "config" / "config.yaml"
_DEFAULT_AC_ENDPOINT = "http://localhost:8000/process_turn"
_DEFAULT_TIMEOUT_S = 30.0


def _load_config() -> tuple[str, float]:
    """
    Read agent_core_client.endpoint and timeout_s from reach_layer/config/config.yaml.
    Returns (endpoint, timeout_s). Falls back to defaults on any error.
    """
    try:
        if not _CONFIG_PATH.exists():
            return _DEFAULT_AC_ENDPOINT, _DEFAULT_TIMEOUT_S
        with _CONFIG_PATH.open("r") as f:
            config = yaml.safe_load(f) or {}
        client_cfg = config.get("agent_core_client", {})
        endpoint = client_cfg.get("endpoint", _DEFAULT_AC_ENDPOINT)
        timeout_s = float(client_cfg.get("timeout_s", _DEFAULT_TIMEOUT_S))
        return endpoint, timeout_s
    except Exception as e:
        logger.warning(
            "reach_layer.config_load_failed",
            extra={
                "operation": "main._load_config",
                "status": "failure",
                "error": str(e),
            },
        )
        return _DEFAULT_AC_ENDPOINT, _DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------------------
# HTTP turn
# ---------------------------------------------------------------------------


def _send_turn(
    client: httpx.Client,
    endpoint: str,
    session_id: str,
    user_message: str,
    timeout_s: float,
) -> str:
    """
    POST /process_turn and return the response text.
    On failure, returns a safe error string — never raises.
    """
    try:
        response = client.post(
            endpoint,
            json={
                "session_id": session_id,
                "user_message": user_message,
                "channel": "cli",
            },
            timeout=timeout_s,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("response_text", "(no response)")

    except httpx.TimeoutException:
        return "[Error: Agent Core did not respond in time. Is it running?]"

    except httpx.ConnectError:
        return f"[Error: Could not connect to Agent Core at {endpoint}. Is it running?]"

    except httpx.HTTPStatusError as e:
        return f"[Error: Agent Core returned HTTP {e.response.status_code}]"

    except Exception as e:
        return f"[Error: {type(e).__name__}: {e}]"


# ---------------------------------------------------------------------------
# CLI REPL
# ---------------------------------------------------------------------------


def main() -> None:
    ac_endpoint, timeout_s = _load_config()
    session_id = str(uuid.uuid4())

    print()
    print("=" * 60)
    print("  Kaam Ki Baat — PoC CLI")
    print(f"  Agent Core:  {ac_endpoint}")
    print(f"  Session ID:  {session_id}")
    print("  Type your message. Ctrl-C or Ctrl-D to exit.")
    print("=" * 60)
    print()

    logger.info(
        "reach_layer.startup",
        extra={
            "operation": "main.main",
            "status": "success",
            "session_id": session_id,
            "endpoint": ac_endpoint,
        },
    )

    with httpx.Client() as client:
        while True:
            try:
                user_input = input("You: ").strip()
            except EOFError:
                print("\nSession ended.")
                break
            except KeyboardInterrupt:
                print("\nSession interrupted.")
                break

            if not user_input:
                continue

            response_text = _send_turn(client, ac_endpoint, session_id, user_input, timeout_s)
            print(f"\nAssistant: {response_text}\n")


if __name__ == "__main__":
    main()
