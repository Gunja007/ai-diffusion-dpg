# =============================================================================
# Knowledge Engine DPG — Multi-stage Dockerfile
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
RUN addgroup --system knowledgeengine && adduser --system --ingroup knowledgeengine knowledgeengine

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source, entrypoint, config, and scripts
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY main.py ./
COPY config/ ./config/

# Ownership
RUN chown -R knowledgeengine:knowledgeengine /app

USER knowledgeengine

# No API keys baked into the image.
# OPENAI_API_KEY (if needed) must be injected at runtime via environment variable.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ChromaDB vector store is written here at ingest time.
# Mount a named volume at this path to persist embeddings across container restarts.
VOLUME /app/data/chroma_db

EXPOSE 8001

CMD ["python", "main.py"]
