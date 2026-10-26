FROM node:22-bookworm-slim AS javascript
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    VIDEO_DATA_DIR=/data VIDEO_NODE=/usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && mkdir /data && chown app:app /data
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements*.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app server.py engine.py platforms.py threads_adapter.py healthcheck.py index.html styles.css app.js ./
USER app
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 CMD ["python", "healthcheck.py"]
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8766"]
