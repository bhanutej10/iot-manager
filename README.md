# IoT Device Manager

A full-stack IoT simulation platform that models a network of 10 virtual environmental sensors. The system handles real-time data collection, multi-protocol communication, time-series storage, and live visualization through a browser-based dashboard.

---

## What It Does

A bash script continuously simulates 10 different sensor types, generating realistic readings for environmental parameters like temperature, pressure, humidity, soil conditions, air quality, and more. These readings are published over MQTT every few seconds, picked up by a Flask backend, stored in a database, analyzed with Pandas, and pushed to a live dashboard via WebSocket.

The entire stack runs in Docker with four containers — a Mosquitto MQTT broker, a Python backend, a sensor simulator, and an Nginx reverse proxy serving the frontend.

---

## Sensor Network

Ten virtual devices are deployed across different physical locations, each measuring a specific set of environmental parameters.

| Device | Type | Location | Key Measurements |
|--------|------|----------|-----------------|
| dev-001 | Temperature | Server Room | temperature_celsius, heat_index, dew_point |
| dev-002 | Pressure | Boiler Room | pressure_hpa, pressure_psi, altitude_m |
| dev-003 | Humidity | Greenhouse | relative_humidity_percent, vapor_pressure_kpa |
| dev-004 | Moisture | Agricultural Field | soil_moisture_percent, water_retention_percent |
| dev-005 | Wind Speed | Rooftop | wind_speed_kmh, wind_direction_deg, beaufort_scale |
| dev-006 | Rainfall | Weather Station | rainfall_mm, rainfall_rate_mm_per_hr, daily_total_mm |
| dev-007 | Light Intensity | Solar Farm | light_lux, uv_index, solar_radiation_w_m2 |
| dev-008 | CO2 | Factory Floor | co2_ppm, pm25_ug_m3, air_quality_index |
| dev-009 | Noise Level | Industrial Zone | noise_db, peak_db, leq_db |
| dev-010 | Soil pH | Greenhouse B | soil_ph, electrical_conductivity, fertility_index |

All devices publish to: `iot/sensors/{sensor_type}/{device_id}/telemetry`

---

## Stack

### Python Libraries

| Library | Version | Role |
|---------|---------|------|
| Flask + Flask-SocketIO | 3.x / 5.3.6 | REST API and WebSocket server |
| paho-mqtt | 2.1.0 | MQTT client connecting to Mosquitto |
| SQLAlchemy | 2.0.30 | ORM with 6 models, SQLite backend |
| Pandas | 2.2.2 | Analytics — statistics, anomaly detection, correlation |
| Matplotlib | 3.8.4 | Server-side PNG chart generation |
| Plotly | 5.22.0 | Interactive chart data for the browser |
| Scapy | 2.5.0 | Network packet crafting and analysis |

### Protocols

| Protocol | How It Is Used | Port |
|----------|---------------|------|
| MQTT | Sensor telemetry and device commands | 1883 (TCP), 9001 (WebSocket) |
| HTTP/REST | Device management and data retrieval — 32 endpoints | 5000 |
| WebSocket | Real-time event push to the dashboard | 80 (via Nginx proxy) |

### Docker Services

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| iot-mosquitto | eclipse-mosquitto:2.0 | 1883, 9001 | MQTT broker |
| iot-backend | python:3.11-slim | 5000 | Flask API + WebSocket |
| iot-simulator | alpine:3.19 | — | Runs the sensor simulation script |
| iot-nginx | nginx:1.25-alpine | 80 | Serves dashboard, proxies API |

---

## Project Structure

```
iot-manager/
├── docker-compose.yml
├── backend/
│   ├── app.py
│   ├── requirements.txt
│   ├── Dockerfile
│   └── mqtt_subscriber.py
├── scripts/
│   └── simulate_sensors.sh
├── docker/
│   ├── mosquitto.conf
│   └── nginx.conf
└── frontend/
    └── index.html
```

---

## Getting Started

### Requirements

- Ubuntu 20.04 / 22.04 / 24.04
- Docker Engine with Docker Compose v2
- Python 3 with pip
- mosquitto-clients

### System dependencies

```bash
sudo apt install -y libpcap-dev libfreetype6-dev mosquitto-clients
```

### Run

```bash
git clone https://github.com/bhanutej10/iot-manager.git
cd iot-manager

# Stop anything already using ports 80 or 1883
sudo systemctl stop apache2 nginx mosquitto 2>/dev/null || true

# Build and start
docker compose up --build
```

First build takes around 3 to 5 minutes. Open the dashboard at `http://localhost`.

---

## Dashboard

The dashboard shows a live overview of all 10 sensors — device status, temperature history, battery levels, active alerts, MQTT message log, WebSocket event stream, and charts rendered by both Matplotlib and Plotly.

---

## API

Base URL: `http://localhost:5000`

```
GET  /health
GET  /api/devices
GET  /api/devices/<id>/readings
POST /api/devices/<id>/command
GET  /api/alerts
POST /api/alerts/acknowledge_all
GET  /api/analytics/summary
GET  /api/analytics/anomalies
GET  /api/analytics/correlation
GET  /api/charts/dashboard.png
GET  /api/plotly/timeseries
POST /api/scapy/craft
GET  /api/mqtt/status
```

---

## MQTT

```bash
# Subscribe to all sensor telemetry
mosquitto_sub -h localhost -t "iot/sensors/#" -v

# Subscribe to a specific sensor type
mosquitto_sub -h localhost -t "iot/sensors/temperature/#" -v
```

---

## Alert Thresholds

The system fires warnings and critical alerts when readings cross these limits.

| Sensor | Metric | Warning | Critical | Direction |
|--------|--------|---------|----------|-----------|
| Temperature | temperature_celsius | 55°C | 70°C | above |
| Pressure | pressure_hpa | 970 hPa | 962 hPa | below |
| Humidity | relative_humidity_percent | 85% | 95% | above |
| Moisture | soil_moisture_percent | 15% | 8% | below |
| Wind Speed | wind_speed_kmh | 62 km/h | 89 km/h | above |
| Rainfall | rainfall_rate_mm_per_hr | 10 | 50 | above |
| Light | light_lux | 100,000 | 115,000 | above |
| CO2 | co2_ppm | 1,000 ppm | 2,000 ppm | above |
| Noise | noise_db | 75 dB | 90 dB | above |
| Soil pH | soil_ph | 5.5 | 4.5 | below |
| All devices | battery_percent | 20% | 10% | below |

Alerts are deduplicated with a 5-minute window and pushed live to the dashboard.

---

## Common Commands

```bash
docker compose logs -f              # live logs
docker compose ps                   # container status
docker compose restart backend      # restart one service
docker compose down                 # stop everything
docker compose down -v              # stop and wipe the database
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
