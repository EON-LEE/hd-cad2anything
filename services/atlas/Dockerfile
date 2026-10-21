FROM node:22-slim AS web
WORKDIR /web
COPY apps/atlas-web/package.json ./
RUN npm install --no-audit --no-fund
COPY apps/atlas-web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ATLAS_WEB_DIR=/app/web

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[api,agents,azure,mcp,db]" && useradd --create-home --uid 10001 app
COPY --from=web /web/dist ./web
USER app

EXPOSE 8000
CMD ["uvicorn", "cad2anything.services.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
