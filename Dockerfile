# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CACHE_DB_PATH=/app/cache/cache.sqlite3

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY convert_ecdict.py .

# The dictionary data is intentionally not part of the source tree. It is
# downloaded and converted at image build time instead.
ARG ECDICT_URL=https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv
ARG WORDNET_URL=https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/wordnet.zip
RUN mkdir -p data/nltk_data/corpora \
    && curl -L --fail --retry 3 --retry-all-errors -o /tmp/ecdict.csv "$ECDICT_URL" \
    && python convert_ecdict.py /tmp/ecdict.csv -o data/ecdict.sqlite3 \
    && rm -f /tmp/ecdict.csv \
    && curl -L --fail --retry 3 --retry-all-errors -o /tmp/wordnet.zip "$WORDNET_URL" \
    && unzip -q /tmp/wordnet.zip -d data/nltk_data/corpora \
    && rm -f /tmp/wordnet.zip \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/cache \
    && chown -R appuser:appuser /app

COPY app.py .
COPY static ./static

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
