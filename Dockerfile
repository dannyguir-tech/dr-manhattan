FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml README.md ./
RUN uv pip install --system ".[mcp]"
COPY . .
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-m", "dr_manhattan.mcp.server_sse"]
