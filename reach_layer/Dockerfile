# =============================================================================
# Reach Layer DPG — CLI variant Dockerfile
# =============================================================================
# This is the CLI (stdin/stdout) channel adapter used in the PoC.
# It is an interactive process, not a server — run with -it flags.
#
# In production the Reach Layer is replaced by a channel adapter
# (WhatsApp, Web, VOIP) that runs as a long-lived HTTP service.
#
# Usage (interactive):
#   docker run -it --network dpg_net reach_layer
#
# Prerequisites: all backend services must be running and reachable on the
# Docker network before starting this container.
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

RUN addgroup --system reachlayer && adduser --system --ingroup reachlayer reachlayer

WORKDIR /app

COPY --from=builder /install /usr/local

COPY main.py ./
COPY config/ ./config/

RUN chown -R reachlayer:reachlayer /app

USER reachlayer

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# No EXPOSE — this is a CLI client, not a server.
# stdin/stdout are the channel. Run with: docker run -it <image>

CMD ["python", "main.py"]
