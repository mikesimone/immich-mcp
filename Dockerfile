FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY immich_mcp ./immich_mcp
RUN pip install --no-cache-dir . && useradd --system --uid 10001 immich-mcp && mkdir -p /data && chown immich-mcp /data
USER immich-mcp
ENV HOST=0.0.0.0 PORT=8099 STATE_DIR=/data
EXPOSE 8099
ENTRYPOINT ["immich-mcp"]
