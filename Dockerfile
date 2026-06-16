FROM python:3.12-slim

# lxml needs libxml2/libxslt at runtime; build essentials kept minimal.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    CRAWLER_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY crawler/ ./crawler/
COPY web/ ./web/

VOLUME ["/data"]
EXPOSE 8000

# Default: serve the search UI. Override the command to run a crawl, e.g.
#   docker run ... python -m crawler crawl https://example.com
CMD ["python", "-m", "crawler", "serve", "--host", "0.0.0.0", "--port", "8000"]
