# Hosted Inbox Triage. See docs/hosting.md. Put HTTPS in front of port 8765.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev
RUN useradd --create-home --uid 10001 triage && mkdir /data && chown triage /data
USER triage
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["uv", "run", "--no-sync", "inbox-triage-web", "--host", "0.0.0.0", "--no-browser", \
            "--config-dir", "/data/config", "--state-dir", "/data/state"]
