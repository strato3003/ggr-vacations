# Playwright 1.50 — aligné sur l'image Docker mcr.microsoft.com/playwright/python:v1.50.0-noble
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
COPY recorder ./recorder
COPY config ./config

RUN pip install --no-cache-dir -e . \
    && mkdir -p /data /config \
    && chown -R pwuser:pwuser /app /data

USER pwuser
ENV GGR_DATA_DIR=/data \
    GGR_CONFIG=/config/config.yaml \
    PYTHONUNBUFFERED=1

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
