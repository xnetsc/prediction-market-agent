FROM python:3.13-slim

WORKDIR /app
COPY . /app
RUN python -m pip install --no-cache-dir . && chmod +x /app/deploy/container-entrypoint.sh /app/deploy/container-with-worker.sh

ENV PREDICTION_AGENT_WORKDIR=/data
ENV FORWARDED_ALLOW_IPS=127.0.0.1,100.0.0.0/8
VOLUME ["/data"]
EXPOSE 8765

ENTRYPOINT ["/app/deploy/container-entrypoint.sh"]
