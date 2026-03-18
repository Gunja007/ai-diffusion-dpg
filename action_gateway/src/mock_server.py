"""
action_gateway/src/mock_server.py

Mock ONEST FastAPI server — PoC stub for external connector API.

Runs on port 9999 (configurable). Single endpoint:
    POST /onest/market_lookup

Returns hardcoded fixture JSON for KKB demo trades (electrician, welder, fitter).
Falls back to a default fixture for any other trade.

Fixture data sourced from KKB PoC plan (Task 2.6):
    electrician → ₹15k–₹28k, steady signal 12% QoQ
    welder      → ₹13k–₹22k, 8% QoQ
    fitter      → ₹14k–₹24k, 10% QoQ
    default     → ₹12k–₹20k, stable

Run standalone:
    python -m action_gateway.src.mock_server      (from repo root)
    python main.py                                 (from action_gateway/ directory)
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded fixture data — from KKB PoC plan Task 2.6
# ---------------------------------------------------------------------------

_FIXTURES: dict[str, dict] = {
    "electrician": {
        "trade": "electrician",
        "salary_range": "₹15k–₹28k",
        "market_signal": "steady signal 12% QoQ",
        "top_employers": ["Hubli Distribution Co", "Karnataka Power"],
        "source": "ONEST",
    },
    "welder": {
        "trade": "welder",
        "salary_range": "₹13k–₹22k",
        "market_signal": "8% QoQ",
        "top_employers": ["Hubli Iron Works", "Dharwad Fabrication"],
        "source": "ONEST",
    },
    "fitter": {
        "trade": "fitter",
        "salary_range": "₹14k–₹24k",
        "market_signal": "10% QoQ",
        "top_employers": ["BEML Hubli", "KA Manufacturing"],
        "source": "ONEST",
    },
}

_DEFAULT_FIXTURE: dict = {
    "trade": "unknown",
    "salary_range": "₹12k–₹20k",
    "market_signal": "stable",
    "top_employers": [],
    "source": "ONEST",
}


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class MarketLookupRequest(BaseModel):
    trade: str
    location: str = ""
    distance_km: int = 50


class MarketLookupResponse(BaseModel):
    trade: str
    salary_range: str
    market_signal: str
    top_employers: list[str]
    source: str
    location_queried: str


class HealthResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_mock_server() -> FastAPI:
    """Create and return the FastAPI mock ONEST server."""

    app = FastAPI(
        title="ONEST Mock Server",
        description="Synthetic ONEST job market data for KKB PoC Action Gateway stub.",
        version="0.1.0",
        docs_url="/docs",
    )

    @app.post("/onest/market_lookup", response_model=MarketLookupResponse)
    def market_lookup(request: MarketLookupRequest) -> MarketLookupResponse:
        """
        Return hardcoded market data for the given trade.

        Looks up trade (case-insensitive) in fixture dict.
        Falls back to default fixture for unknown trades.
        """
        trade_key = request.trade.lower().strip()
        fixture = _FIXTURES.get(trade_key, _DEFAULT_FIXTURE)

        logger.info(
            "mock_server.market_lookup",
            extra={
                "operation": "mock_server.market_lookup",
                "status": "success",
                "trade": trade_key,
                "location": request.location,
                "fixture_matched": trade_key in _FIXTURES,
            },
        )

        return MarketLookupResponse(
            trade=fixture["trade"],
            salary_range=fixture["salary_range"],
            market_signal=fixture["market_signal"],
            top_employers=fixture["top_employers"],
            source=fixture["source"],
            location_queried=request.location or "not specified",
        )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Readiness probe."""
        return HealthResponse(status="ok")

    return app


# Module-level app instance for uvicorn
app = create_mock_server()
