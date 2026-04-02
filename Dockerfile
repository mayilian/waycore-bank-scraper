# Multi-arch build: supports amd64 and arm64 (Graviton)
# Build: docker buildx build --platform linux/amd64,linux/arm64 -t waycore-worker .
# Graviton (arm64) is 20% cheaper on Fargate.
FROM python:3.12-slim

# System deps for Playwright Chromium on headless Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    # Playwright Chromium deps (subset of `playwright install-deps`)
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libx11-xcb1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Python dependencies first (layer cache)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --extra anthropic

# Install Playwright Chromium (auto-detects arch: amd64 or arm64)
RUN uv run playwright install chromium

COPY . .

ENV PYTHONUNBUFFERED=1 \
    SCREENSHOT_DIR=/app/data/screenshots

RUN mkdir -p /app/data/screenshots

EXPOSE 9000

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD curl -sf http://localhost:9000/restate/health || exit 1

CMD ["uv", "run", "hypercorn", "src.worker.app:app", "--bind", "0.0.0.0:9000"]
