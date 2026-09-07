FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY static/ ./static/
COPY requirements-min.txt ./
RUN pip install --no-cache-dir -r requirements-min.txt

ENV PYTHONPATH=/app/src
EXPOSE 8090
CMD ["python3", "scripts/ap_gateway.py", "--camera", "192.168.169.1:6123", "--listen", "0.0.0.0", "--port", "8090"]
