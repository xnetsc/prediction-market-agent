FROM node:22-bookworm-slim AS clients
RUN npm install --global @openai/codex @anthropic-ai/claude-code && npm cache clean --force

FROM python:3.13-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/xnetsc/prediction-market-agent"
LABEL org.opencontainers.image.licenses="LicenseRef-PolyForm-Noncommercial-1.0.0"

COPY --from=clients /usr/local/bin/node /usr/local/bin/node
COPY --from=clients /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && ln -s "/usr/local/lib/node_modules/@anthropic-ai/claude-code/$(node -p 'require("/usr/local/lib/node_modules/@anthropic-ai/claude-code/package.json").bin.claude')" /usr/local/bin/claude \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml LICENSE README.md /app/
COPY src /app/src
COPY deploy/container-entrypoint.sh deploy/container-with-worker.sh /app/deploy/
RUN python -m pip install --no-cache-dir . && chmod +x /app/deploy/container-entrypoint.sh /app/deploy/container-with-worker.sh

ENV PREDICTION_AGENT_WORKDIR=/data
ENV FORWARDED_ALLOW_IPS=127.0.0.1,100.0.0.0/8
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/healthz',timeout=4)"

ENTRYPOINT ["/app/deploy/container-entrypoint.sh"]
