# ── Backend Dockerfile ──────────────────────────────────────
FROM python:3.11-slim

LABEL maintainer="IoT Manager"
LABEL description="Flask backend: paho-mqtt, SQLAlchemy, Pandas, Matplotlib, Plotly, Scapy"

# System deps for Scapy (raw sockets) and Matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpcap-dev \
        libffi-dev \
        libssl-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create data directories
RUN mkdir -p /app/data /app/data/charts

EXPOSE 5000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

CMD ["python", "app.py"]
