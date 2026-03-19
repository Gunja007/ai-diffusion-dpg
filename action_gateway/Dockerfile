# =============================================================================
# Action Gateway DPG — Multi-stage Dockerfile
# =============================================================================
# Runs the mock ONEST FastAPI server on port 9999.
# In production this is replaced by a real connector to the ONEST network.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

RUN pip install --upgrade pip setuptools wheel

COPY pyproject.toml ./

RUN pip install --prefix=/install --no-cache-dir .

# -----------------------------------------------------------------------------
# Stage 2: runtime
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

RUN addgroup --system actiongateway && adduser --system --ingroup actiongateway actiongateway

WORKDIR /app

COPY --from=builder /install /usr/local

# Copy source package and entrypoint
# src/ must be present so `from src.mock_server import app` resolves at startup
COPY src/ ./src/
COPY main.py ./
COPY config/ ./config/

RUN chown -R actiongateway:actiongateway /app

USER actiongateway

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 9999

CMD ["python", "main.py"]
