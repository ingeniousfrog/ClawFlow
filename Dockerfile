FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Copy source
COPY nanoclaw/ nanoclaw/
COPY skills/ skills/

# Create directories
RUN mkdir -p /app/workspace /app/config /app/data

# Volumes
VOLUME /app/workspace
VOLUME /app/config
VOLUME /app/data

# Dashboard port (localhost only by default)
EXPOSE 18790

# Environment
ENV NANOCLAW_HOME=/app
ENV NANOCLAW_WORKSPACE=/app/workspace
ENV NANOCLAW_CONFIG=/app/config/config.json

CMD ["nanoclaw", "serve"]
