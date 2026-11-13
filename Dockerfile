FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

# /data holds the database, backups and the Garmin token store (a volume on the server)
RUN useradd --uid 1000 --create-home dromos && mkdir /data && chown dromos /data
USER dromos
ENV DROMOS_DB=/data/dromos.db \
    DROMOS_BACKUP_DIR=/data/backups \
    GARMINTOKENS=/data/garmin-tokens

EXPOSE 8000
HEALTHCHECK --interval=1m --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

# One worker only: background jobs run inside the app process.
CMD ["uvicorn", "dromos.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
