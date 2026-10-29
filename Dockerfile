# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HTTP_HOST=0.0.0.0 HTTP_PORT=8080 DB_PATH=/data/storeroom.db
WORKDIR /app
COPY requirements.txt .
# The optional CA secret supports builds behind a trusted development proxy.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r requirements.txt
RUN groupadd --gid 10001 storeroom && useradd --uid 10001 --gid storeroom storeroom \
    && mkdir /data && chown storeroom:storeroom /data
COPY --chmod=0444 server.py dashboard.html backup.py ./
USER storeroom
EXPOSE 8080
CMD ["python", "server.py"]
