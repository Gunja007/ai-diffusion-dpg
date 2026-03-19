# =============================================================================
# Trust Layer DPG — Multi-stage Dockerfile
# =============================================================================
# Stage 1 (builder): install dependencies into an isolated prefix.
# Stage 2 (runtime): copy only the installed packages and source — no build tools.
# Mirrors agent_core/Dockerfile conventions exactly.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tooling
RUN pip install --upgrade pip setuptools wheel

# Copy dependency declarations first — layer is cached unless pyproject.toml changes
COPY pyproject.toml ./

# Install runtime dependencies into /install prefix (no dev extras)
RUN pip install --prefix=/install --no-cache-dir .

# -----------------------------------------------------------------------------
# Stage 2: runtime
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN addgroup --system trustlayer && adduser --system --ingroup trustlayer trustlayer

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source, entrypoint, and config
COPY src/ ./src/
COPY main.py ./
COPY config/ ./config/

# Ownership
RUN chown -R trustlayer:trustlayer /app

USER trustlayer

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8003

CMD ["python", "main.py"]
