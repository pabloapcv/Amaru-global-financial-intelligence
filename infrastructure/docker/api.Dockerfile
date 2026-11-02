# Amaru API, ingestion scheduler and CLI (same image, different commands).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Pango/HarfBuzz for native PDF reports (WeasyPrint); fonts for readable output.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[postgres,pdf]"

RUN useradd --create-home --uid 1000 atlas && mkdir -p /data && chown atlas /data
USER atlas
ENV ATLAS_OBJECT_STORE=/data/objects

EXPOSE 8010
CMD ["uvicorn", "atlas.api.main:app", "--host", "0.0.0.0", "--port", "8010", "--proxy-headers"]
