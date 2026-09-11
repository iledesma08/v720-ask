# Camera gateway image (Pi 5 / aarch64, but generic).
# Host network mode is used at runtime (see docker-compose.cam.yml):
# the container must reach the camera AP subnet 192.168.169.0/24 through
# the host's wlan0, which NATed bridge networking cannot guarantee
# against the camera's minimal TCP stack.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# curl: needed for the compose healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-min.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-min.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY static/ ./static/

EXPOSE 8090 8000
