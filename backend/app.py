"""
IoT Device Manager — backend/app.py
====================================
Libraries  : Flask, Flask-SocketIO, paho-mqtt, SQLAlchemy,
             Pandas, Matplotlib, Plotly, Scapy
Protocols  : MQTT (paho), HTTP/REST (Flask), WebSocket (Socket.IO)
Database   : SQLite via SQLAlchemy (swap DATABASE_URL for Postgres)
"""

# Eventlet monkey-patch — must be first line before all other imports
import eventlet
eventlet.monkey_patch()

import base64
import io
import json
import logging
import os
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ── Flask + WebSocket ─────────────────────────────────────────────────────────
from flask import Flask, jsonify, request, abort, send_file
from flask_socketio import SocketIO, emit

# ── SQLAlchemy ORM ────────────────────────────────────────────────────────────
from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    DateTime, Boolean, Text, JSON, func,
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

# ── Pandas ────────────────────────────────────────────────────────────────────
import pandas as pd

# ── Matplotlib (headless) ─────────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.gridspec import GridSpec

# ── Plotly ────────────────────────────────────────────────────────────────────
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# ── paho-mqtt ─────────────────────────────────────────────────────────────────
import paho.mqtt.client as mqtt_client

# ── Scapy (packet crafting + analysis) ───────────────────────────────────────
try:
    from scapy.all import IP, TCP, UDP, Raw
    SCAPY_AVAILABLE = True
except Exception:
    SCAPY_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("iot")

# ─────────────────────────────────────────────────────────────────────────────
# FLASK + SOCKET.IO SETUP
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "iot-secret-key-2024")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25,
    allow_upgrades=True,
)

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE — SQLAlchemy
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
DB_PATH   = os.path.join(DATA_DIR, "iot.db")
CHART_DIR = os.path.join(DATA_DIR, "charts")
os.makedirs(DATA_DIR,  exist_ok=True)
os.makedirs(CHART_DIR, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False,
    pool_pre_ping=True,
)
Base    = declarative_base()
Session = scoped_session(sessionmaker(bind=engine))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── ORM Models ────────────────────────────────────────────────────────────────

class Device(Base):
    __tablename__ = "devices"
    device_id      = Column(String(32),  primary_key=True)
    device_type    = Column(String(64))
    location       = Column(String(128))
    status         = Column(String(32),  default="online")
    registered_at  = Column(DateTime,    default=_now)
    firmware       = Column(String(32))
    battery        = Column(Integer)
    signal_rssi    = Column(Integer)
    uptime_seconds = Column(Integer)
    last_seen      = Column(DateTime)
    alerts_enabled = Column(Boolean,     default=True)
    ip_address     = Column(String(32))
    mac_address    = Column(String(32))


class SensorReading(Base):
    __tablename__ = "sensor_readings"
    id        = Column(Integer,   primary_key=True, autoincrement=True)
    device_id = Column(String(32), index=True)
    timestamp = Column(DateTime,   default=_now, index=True)
    readings  = Column(JSON)


class Alert(Base):
    __tablename__ = "alerts"
    id           = Column(Integer,  primary_key=True, autoincrement=True)
    device_id    = Column(String(32), index=True)
    metric       = Column(String(64))
    value        = Column(Float)
    threshold    = Column(Float)
    direction    = Column(String(8))
    severity     = Column(String(16))
    message      = Column(Text)
    triggered_at = Column(DateTime,  default=_now)
    acknowledged = Column(Boolean,   default=False)


class Command(Base):
    __tablename__ = "commands"
    id          = Column(Integer,   primary_key=True, autoincrement=True)
    device_id   = Column(String(32))
    command     = Column(String(64))
    payload     = Column(JSON)
    status      = Column(String(16), default="pending")
    created_at  = Column(DateTime,   default=_now)
    executed_at = Column(DateTime)


class MqttMessage(Base):
    __tablename__ = "mqtt_messages"
    id          = Column(Integer,   primary_key=True, autoincrement=True)
    topic       = Column(String(256))
    payload     = Column(Text)
    direction   = Column(String(4),  default="in")   # in | out
    received_at = Column(DateTime,   default=_now)


class PacketLog(Base):
    __tablename__ = "packet_log"
    id          = Column(Integer,   primary_key=True, autoincrement=True)
    src_ip      = Column(String(32))
    dst_ip      = Column(String(32))
    protocol    = Column(String(8))
    src_port    = Column(Integer)
    dst_port    = Column(Integer)
    length      = Column(Integer)
    summary     = Column(Text)
    captured_at = Column(DateTime,  default=_now)


Base.metadata.create_all(engine)
log.info("[DB] Tables created/verified on: %s", DATABASE_URL)

# ─────────────────────────────────────────────────────────────────────────────
# DEVICE PROFILES & SENSOR RANGES
# ─────────────────────────────────────────────────────────────────────────────

# ── 10 dedicated sensors (mandatory 4 + 6 similar physical) ──
# Format: (device_id, sensor_type, location, ip, mac)
DEVICE_PROFILES = [
    # ── Mandatory 4 ──────────────────────────────────────────
    ("dev-001", "temperature",     "server_room",       "192.168.1.101", "AA:BB:CC:DD:EE:01"),
    ("dev-002", "pressure",        "boiler_room",       "192.168.1.102", "AA:BB:CC:DD:EE:02"),
    ("dev-003", "humidity",        "greenhouse",        "192.168.1.103", "AA:BB:CC:DD:EE:03"),
    ("dev-004", "moisture",        "agricultural_field","192.168.1.104", "AA:BB:CC:DD:EE:04"),
    # ── Additional 6 (same physical/environmental domain) ────
    ("dev-005", "wind_speed",      "rooftop",           "192.168.1.105", "AA:BB:CC:DD:EE:05"),
    ("dev-006", "rainfall",        "weather_station",   "192.168.1.106", "AA:BB:CC:DD:EE:06"),
    ("dev-007", "light_intensity", "solar_farm",        "192.168.1.107", "AA:BB:CC:DD:EE:07"),
    ("dev-008", "co2",             "factory_floor",     "192.168.1.108", "AA:BB:CC:DD:EE:08"),
    ("dev-009", "noise_level",     "industrial_zone",   "192.168.1.109", "AA:BB:CC:DD:EE:09"),
    ("dev-010", "soil_ph",         "greenhouse_b",      "192.168.1.110", "AA:BB:CC:DD:EE:10"),
]

# ── Sensor metric ranges — primary + related secondary metrics ─
SENSOR_RANGES = {
    # ── MANDATORY 1: Temperature sensor ──────────────────────
    "temperature": {
        "temperature_celsius":         (15.0, 75.0),
        "temperature_fahrenheit":      (59.0, 167.0),
        "heat_index_celsius":          (18.0, 80.0),
        "dew_point_celsius":           (5.0,  30.0),
        "rate_of_change_c_per_min":    (-0.5,  0.5),
    },
    # ── MANDATORY 2: Pressure sensor ─────────────────────────
    "pressure": {
        "pressure_hpa":                (960.0,  1050.0),
        "pressure_psi":                (13.9,   15.2),
        "pressure_bar":                (0.960,  1.050),
        "altitude_m":                  (-400.0, 500.0),
        "pressure_trend_hpa_per_hr":   (-2.0,   2.0),
    },
    # ── MANDATORY 3: Humidity sensor ─────────────────────────
    "humidity": {
        "relative_humidity_percent":   (20.0,  99.0),
        "absolute_humidity_g_per_m3":  (4.3,   21.5),
        "specific_humidity_kg_per_kg": (0.124, 0.616),
        "vapor_pressure_kpa":          (0.5,   4.2),
        "comfort_index":               (0.0,   100.0),
    },
    # ── MANDATORY 4: Moisture sensor ─────────────────────────
    "moisture": {
        "soil_moisture_percent":       (5.0,  60.0),
        "moisture_raw_adc":            (200.0, 900.0),
        "water_retention_percent":     (10.0,  85.0),
        "field_capacity_percent":      (25.0,  45.0),
        "wilting_point_percent":       (5.0,   15.0),
    },
    # ── ADDITIONAL 5: Wind speed sensor ──────────────────────
    "wind_speed": {
        "wind_speed_kmh":              (0.0,   120.0),
        "wind_speed_ms":               (0.0,   33.3),
        "wind_direction_deg":          (0.0,   359.0),
        "gust_speed_kmh":              (0.0,   150.0),
        "beaufort_scale":              (0.0,   12.0),
    },
    # ── ADDITIONAL 6: Rainfall sensor ────────────────────────
    "rainfall": {
        "rainfall_mm":                 (0.0,  50.0),
        "rainfall_rate_mm_per_hr":     (0.0,  100.0),
        "daily_total_mm":              (0.0,  200.0),
        "weekly_total_mm":             (0.0,  500.0),
    },
    # ── ADDITIONAL 7: Light intensity sensor ─────────────────
    "light_intensity": {
        "light_lux":                   (0.0,   120000.0),
        "uv_index":                    (0.0,   11.0),
        "par_umol_m2_s":               (0.0,   2200.0),
        "solar_radiation_w_m2":        (0.0,   1000.0),
        "daylight_hours":              (0.0,   16.0),
    },
    # ── ADDITIONAL 8: CO2 sensor ─────────────────────────────
    "co2": {
        "co2_ppm":                     (350.0,  5000.0),
        "co_ppm":                      (0.0,    100.0),
        "voc_ppb":                     (0.0,    2000.0),
        "pm25_ug_m3":                  (0.0,    300.0),
    },
    # ── ADDITIONAL 9: Noise level sensor ─────────────────────
    "noise_level": {
        "noise_db":                    (20.0,  120.0),
        "peak_db":                     (23.0,  138.0),
        "noise_frequency_hz":          (20.0,  20000.0),
        "leq_db":                      (18.0,  118.0),
    },
    # ── ADDITIONAL 10: Soil pH sensor ────────────────────────
    "soil_ph": {
        "soil_ph":                     (4.0,  9.0),
        "soil_temperature_celsius":    (5.0,  40.0),
        "electrical_conductivity_ms_cm":(0.1, 4.0),
        "organic_matter_percent":      (0.5,  8.0),
        "fertility_index":             (0.0,  100.0),
    },
}

# ── Alert thresholds — primary metric per sensor ──────────────
THRESHOLDS = {
    # Mandatory sensors
    "temperature_celsius":          {"warning": 55.0,  "critical": 70.0,  "direction": "above"},
    "relative_humidity_percent":    {"warning": 85.0,  "critical": 95.0,  "direction": "above"},
    "pressure_hpa":                 {"warning": 970.0, "critical": 962.0, "direction": "below"},
    "soil_moisture_percent":        {"warning": 15.0,  "critical": 8.0,   "direction": "below"},
    # Additional sensors
    "wind_speed_kmh":               {"warning": 62.0,  "critical": 89.0,  "direction": "above"},
    "rainfall_rate_mm_per_hr":      {"warning": 10.0,  "critical": 50.0,  "direction": "above"},
    "light_lux":                    {"warning": 100000.0, "critical": 115000.0, "direction": "above"},
    "co2_ppm":                      {"warning": 1000.0, "critical": 2000.0, "direction": "above"},
    "noise_db":                     {"warning": 75.0,  "critical": 90.0,  "direction": "above"},
    "soil_ph":                      {"warning": 5.5,   "critical": 4.5,   "direction": "below"},
    # Universal device health
    "battery_percent":              {"warning": 20.0,  "critical": 10.0,  "direction": "below"},
}

# ─────────────────────────────────────────────────────────────────────────────
# SENSOR DATA GENERATOR (drift-based realistic simulation)
# ─────────────────────────────────────────────────────────────────────────────

_drift: dict = defaultdict(lambda: defaultdict(float))


def generate_reading(device_id: str, device_type: str) -> dict:
    ranges = SENSOR_RANGES.get(device_type, {})
    readings = {}
    for metric, (lo, hi) in ranges.items():
        _drift[device_id][metric] += random.gauss(0, 0.05)
        _drift[device_id][metric]  = max(-1.0, min(1.0, _drift[device_id][metric]))
        mid  = (lo + hi) / 2.0
        span = (hi - lo) / 2.0
        val  = mid + _drift[device_id][metric] * span * 0.6 + random.gauss(0, span * 0.05)
        readings[metric] = round(max(lo, min(hi, val)), 3)
    return readings

# ─────────────────────────────────────────────────────────────────────────────
# MQTT — paho-mqtt
# ─────────────────────────────────────────────────────────────────────────────

MQTT_BROKER  = os.getenv("MQTT_BROKER", "mosquitto")
MQTT_PORT    = int(os.getenv("MQTT_PORT",  "1883"))
MQTT_ENABLED = os.getenv("USE_MQTT", "true").lower() == "true"

_mqtt_client = None
_mqtt_connected = False


def _on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    global _mqtt_connected
    if reason_code == 0:
        _mqtt_connected = True
        # Match shell script topic: iot/sensors/{sensor_type}/{device_id}/telemetry
        client.subscribe("iot/sensors/#", qos=1)
        # Also subscribe to legacy device topics and command topics
        client.subscribe("iot/devices/#", qos=1)
        client.subscribe("iot/commands/#", qos=1)
        log.info("[MQTT] Connected to %s:%s — subscribed to iot/sensors/# iot/devices/# iot/commands/#", MQTT_BROKER, MQTT_PORT)
    else:
        log.warning("[MQTT] Connection failed: %s", reason_code)


def _on_mqtt_disconnect(client, userdata, flags, reason_code, properties=None):
    global _mqtt_connected
    _mqtt_connected = False
    log.warning("[MQTT] Disconnected — reconnecting …")


def _on_mqtt_message(client, userdata, msg):
    """Persist every incoming MQTT message to DB and push to WebSocket clients."""
    try:
        payload_str = msg.payload.decode("utf-8", errors="replace")
    except Exception:
        payload_str = str(msg.payload)

    session = Session()
    try:
        session.add(MqttMessage(topic=msg.topic, payload=payload_str[:4000], direction="in"))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()

    socketio.emit("mqtt_message", {
        "topic":   msg.topic,
        "payload": payload_str[:512],
        "ts":      _now().isoformat(),
    })


def mqtt_publish(topic: str, payload) -> bool:
    """Publish a message via paho-mqtt and persist it."""
    if not _mqtt_connected:
        return False
    msg_str = json.dumps(payload) if not isinstance(payload, str) else payload
    _mqtt_client.publish(topic, msg_str, qos=1)
    session = Session()
    try:
        session.add(MqttMessage(topic=topic, payload=msg_str[:4000], direction="out"))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()
    return True


def _init_mqtt():
    global _mqtt_client
    if not MQTT_ENABLED:
        log.info("[MQTT] Disabled (set USE_MQTT=true to enable)")
        return
    _mqtt_client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    _mqtt_client.on_connect    = _on_mqtt_connect
    _mqtt_client.on_disconnect = _on_mqtt_disconnect
    _mqtt_client.on_message    = _on_mqtt_message
    try:
        _mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        _mqtt_client.loop_start()
        log.info("[MQTT] Connecting to %s:%s …", MQTT_BROKER, MQTT_PORT)
    except Exception as e:
        log.warning("[MQTT] Could not connect: %s", e)

# ─────────────────────────────────────────────────────────────────────────────
# SCAPY — packet crafting & analysis
# ─────────────────────────────────────────────────────────────────────────────

_captured_packets: list = []


def _craft_mqtt_packet(src_ip: str, dst_ip: str, payload: str = "{}") -> dict:
    """Craft a simulated MQTT-over-TCP packet using Scapy."""
    raw_payload  = f"MQTT_CONNECT|{payload}".encode()
    sport        = random.randint(40000, 60000)
    base_summary = f"TCP {src_ip}:{sport} → {dst_ip}:1883 [MQTT PUBLISH] len={len(raw_payload)}"
    result = {
        "src_ip": src_ip, "dst_ip": dst_ip,
        "protocol": "TCP", "src_port": sport, "dst_port": 1883,
        "length": len(raw_payload) + 40,
        "summary": base_summary,
        "scapy_available": SCAPY_AVAILABLE,
    }
    if SCAPY_AVAILABLE:
        try:
            pkt = IP(src=src_ip, dst=dst_ip) / TCP(sport=sport, dport=1883, flags="PA") / Raw(load=raw_payload)
            result.update({
                "layers": [l.name for l in pkt.layers()],
                "length": len(pkt),
                "summary": pkt.summary(),
                "hex_dump": pkt.build().hex()[:128] + "…",
            })
        except Exception as e:
            result["scapy_error"] = str(e)
    return result


def _craft_http_packet(src_ip: str, dst_ip: str, path: str = "/api/devices") -> dict:
    """Craft a simulated HTTP/REST packet using Scapy."""
    http_payload = f"GET {path} HTTP/1.1\r\nHost: {dst_ip}\r\n\r\n".encode()
    sport = random.randint(40000, 60000)
    result = {
        "src_ip": src_ip, "dst_ip": dst_ip,
        "protocol": "TCP", "src_port": sport, "dst_port": 80,
        "length": len(http_payload) + 40,
        "summary": f"HTTP GET {path} {src_ip}:{sport} → {dst_ip}:80",
        "scapy_available": SCAPY_AVAILABLE,
    }
    if SCAPY_AVAILABLE:
        try:
            pkt = IP(src=src_ip, dst=dst_ip) / TCP(sport=sport, dport=80, flags="PA") / Raw(load=http_payload)
            result.update({
                "layers":   [l.name for l in pkt.layers()],
                "length":   len(pkt),
                "summary":  pkt.summary(),
                "hex_dump": pkt.build().hex()[:128] + "…",
            })
        except Exception as e:
            result["scapy_error"] = str(e)
    return result


def _analyze_packets(packets: list) -> dict:
    if not packets:
        return {"total_packets": 0, "available": SCAPY_AVAILABLE}
    protos = defaultdict(int)
    ports  = defaultdict(int)
    total_bytes = 0
    for p in packets:
        protos[p.get("protocol", "unknown")] += 1
        ports[str(p.get("dst_port", 0))] += 1
        total_bytes += p.get("length", 0)
    return {
        "available":      SCAPY_AVAILABLE,
        "total_packets":  len(packets),
        "total_bytes":    total_bytes,
        "by_protocol":    dict(protos),
        "by_dst_port":    dict(ports),
        "avg_length":     round(total_bytes / len(packets), 1) if packets else 0,
    }


def _packet_simulation_loop():
    """Background thread: craft and store simulated network packets."""
    while True:
        time.sleep(5)
        profile = random.choice(DEVICE_PROFILES)
        did, dtype, loc, ip, mac = profile
        ptype   = random.choice(["mqtt", "http"])
        broker  = "192.168.1.200"
        if ptype == "mqtt":
            payload = json.dumps({"device_id": did, "type": dtype})
            pkt = _craft_mqtt_packet(ip, broker, payload)
        else:
            path = random.choice(["/api/devices", "/api/alerts", "/health"])
            pkt  = _craft_http_packet(ip, broker, path)

        _captured_packets.append({**pkt, "captured_at": _now().isoformat()})
        if len(_captured_packets) > 500:
            _captured_packets.pop(0)

        session = Session()
        try:
            session.add(PacketLog(
                src_ip=pkt["src_ip"], dst_ip=pkt["dst_ip"],
                protocol=pkt["protocol"], src_port=pkt.get("src_port"),
                dst_port=pkt.get("dst_port"), length=pkt.get("length"),
                summary=pkt.get("summary", "")[:512],
            ))
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

        socketio.emit("packet_update", pkt)

# ─────────────────────────────────────────────────────────────────────────────
# PANDAS — analytics helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_df(device_ids=None, hours: int = 1) -> pd.DataFrame:
    """Load sensor readings into a Pandas DataFrame."""
    session = Session()
    cutoff  = _now() - timedelta(hours=hours)
    q = session.query(SensorReading).filter(SensorReading.timestamp >= cutoff)
    if device_ids:
        q = q.filter(SensorReading.device_id.in_(device_ids))
    rows = q.order_by(SensorReading.timestamp.asc()).all()
    session.close()
    if not rows:
        return pd.DataFrame()
    records = []
    for r in rows:
        rec = {"device_id": r.device_id, "timestamp": r.timestamp}
        if r.readings:
            rec.update(r.readings)
        records.append(rec)
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _pd_summary(df: pd.DataFrame) -> dict:
    """Pandas descriptive statistics — mean, std, min, max, median, p95."""
    if df.empty:
        return {}
    result = {}
    for col in df.select_dtypes(include="number").columns:
        result[col] = {
            "mean":   round(df[col].mean(),           3),
            "std":    round(df[col].std(),            3),
            "min":    round(df[col].min(),            3),
            "max":    round(df[col].max(),            3),
            "median": round(df[col].median(),         3),
            "p95":    round(df[col].quantile(0.95),   3),
        }
    return result


def _pd_device_summary(df: pd.DataFrame) -> list:
    """Per-device mean / max / min aggregation via Pandas groupby."""
    if df.empty or "device_id" not in df.columns:
        return []
    numeric = df.select_dtypes(include="number").columns.tolist()
    if not numeric:
        return []
    g = df.groupby("device_id")[numeric].agg(["mean", "max", "min"]).round(2)
    g.columns = ["_".join(c) for c in g.columns]
    return g.reset_index().to_dict(orient="records")


def _pd_correlation(df: pd.DataFrame) -> dict:
    """Pearson correlation matrix via Pandas."""
    if df.empty:
        return {}
    num = df.select_dtypes(include="number")
    if num.shape[1] < 2:
        return {}
    return num.corr().round(3).to_dict()


def _pd_anomalies(df: pd.DataFrame, z_threshold: float = 2.5) -> list:
    """Z-score anomaly detection using Pandas."""
    if df.empty:
        return []
    anomalies = []
    for col in df.select_dtypes(include="number").columns:
        data = df[["device_id", "timestamp", col]].dropna()
        if len(data) < 5:
            continue
        mean, std = data[col].mean(), data[col].std()
        if std == 0:
            continue
        outliers = data[((data[col] - mean) / std).abs() > z_threshold]
        for _, row in outliers.iterrows():
            anomalies.append({
                "device_id": row["device_id"],
                "metric":    col,
                "value":     round(float(row[col]), 3),
                "z_score":   round(float(abs((row[col] - mean) / std)), 2),
                "timestamp": str(row["timestamp"]),
            })
    return sorted(anomalies, key=lambda x: x["z_score"], reverse=True)[:50]


def _pd_rolling(df: pd.DataFrame, metric: str, window: int = 5) -> list:
    """Rolling window average per device using Pandas."""
    if df.empty or metric not in df.columns:
        return []
    sub = df[["timestamp", "device_id", metric]].dropna().sort_values("timestamp")
    sub = sub.copy()
    sub["rolling_avg"] = (
        sub.groupby("device_id")[metric]
           .transform(lambda x: x.rolling(window, min_periods=1).mean())
           .round(3)
    )
    sub["timestamp"] = sub["timestamp"].astype(str)
    return sub.tail(200).to_dict(orient="records")

# ─────────────────────────────────────────────────────────────────────────────
# MATPLOTLIB — server-side static chart generation
# ─────────────────────────────────────────────────────────────────────────────

_MPL_STYLE = {
    "figure.facecolor": "#0d1117",
    "axes.facecolor":   "#161b22",
    "axes.edgecolor":   "#30363d",
    "axes.labelcolor":  "#8b949e",
    "xtick.color":      "#8b949e",
    "ytick.color":      "#8b949e",
    "grid.color":       "#21262d",
    "text.color":       "#c9d1d9",
    "lines.linewidth":  1.5,
    "grid.linestyle":   "--",
    "grid.alpha":       0.5,
}
_COLORS = [
    "#58a6ff","#3fb950","#f78166","#d2a8ff","#ffa657",
    "#79c0ff","#56d364","#ff7b72","#f0883e","#7ee787",
]


def _mpl_timeseries(device_ids: list, metric: str, hours: int = 1) -> bytes:
    """Generate a time-series line chart PNG using Matplotlib."""
    df = _load_df(device_ids, hours=hours)
    with plt.rc_context(_MPL_STYLE):
        fig, ax = plt.subplots(figsize=(10, 4))
        if df.empty or metric not in df.columns:
            ax.text(0.5, 0.5, f"No data for '{metric}' yet — waiting for readings…",
                    ha="center", va="center", transform=ax.transAxes, color="#8b949e")
        else:
            for i, did in enumerate(device_ids or df["device_id"].unique()):
                sub = df[df["device_id"] == did].dropna(subset=[metric])
                if sub.empty:
                    continue
                ax.plot(sub["timestamp"], sub[metric],
                        label=did, color=_COLORS[i % len(_COLORS)], linewidth=1.5)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            fig.autofmt_xdate()
            ax.set_xlabel("Time (UTC)")
            ax.set_ylabel(metric.replace("_", " ").title())
            ax.set_title(f"{metric.replace('_', ' ').title()} — last {hours}h", color="#c9d1d9", pad=10)
            ax.legend(fontsize=7, framealpha=0.3, ncol=2)
            ax.grid(True)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()


def _mpl_dashboard(hours: int = 1) -> bytes:
    """Multi-panel Matplotlib dashboard PNG using GridSpec."""
    df = _load_df(hours=hours)
    with plt.rc_context(_MPL_STYLE):
        fig = plt.figure(figsize=(14, 8))
        gs  = GridSpec(2, 3, figure=fig, hspace=0.45, wspace=0.35)

        # Panel 1 — Temperature trend (wide)
        ax1 = fig.add_subplot(gs[0, :2])
        if not df.empty and "temperature" in df.columns:
            for i, did in enumerate(df["device_id"].unique()):
                sub = df[df["device_id"] == did].dropna(subset=["temperature"])
                if not sub.empty:
                    ax1.plot(sub["timestamp"], sub["temperature"],
                             label=did.replace("dev-", "D"),
                             color=_COLORS[i % len(_COLORS)], linewidth=1.2)
        ax1.set_title("Temperature (°C)", color="#c9d1d9")
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax1.legend(fontsize=6, framealpha=0.2, ncol=5)
        ax1.grid(True)

        # Panel 2 — CO₂ with threshold lines
        ax2 = fig.add_subplot(gs[0, 2])
        if not df.empty and "co2_ppm" in df.columns:
            sub = df.dropna(subset=["co2_ppm"])
            if not sub.empty:
                ax2.fill_between(sub["timestamp"], sub["co2_ppm"], alpha=0.3, color="#f78166")
                ax2.plot(sub["timestamp"], sub["co2_ppm"], color="#f78166", linewidth=1.2)
                ax2.axhline(1000, color="#ffa657", linestyle="--", linewidth=1, label="Warn 1000")
                ax2.axhline(2000, color="#ff7b72", linestyle="--", linewidth=1, label="Crit 2000")
                ax2.legend(fontsize=7, framealpha=0.2)
        ax2.set_title("CO₂ (ppm)", color="#c9d1d9")
        ax2.grid(True)

        # Panel 3 — Battery horizontal bars
        ax3 = fig.add_subplot(gs[1, 0])
        session = Session()
        devs    = session.query(Device).all()
        session.close()
        if devs:
            ids    = [d.device_id.replace("dev-", "D") for d in devs]
            batts  = [d.battery or 50 for d in devs]
            colors = ["#ff7b72" if b < 10 else "#ffa657" if b < 20 else "#3fb950" for b in batts]
            ax3.barh(ids, batts, color=colors, height=0.6)
            ax3.set_xlim(0, 100)
            ax3.axvline(20, color="#ffa657", linestyle="--", linewidth=1, alpha=0.7)
        ax3.set_title("Battery (%)", color="#c9d1d9")

        # Panel 4 — Alert severity pie
        ax4 = fig.add_subplot(gs[1, 1])
        session = Session()
        raw_a   = session.query(Alert.severity, func.count(Alert.id)).group_by(Alert.severity).all()
        session.close()
        if raw_a:
            labels = [r[0] for r in raw_a]
            vals   = [r[1] for r in raw_a]
            cmap   = {"critical": "#ff7b72", "warning": "#ffa657"}
            ax4.pie(vals, labels=labels,
                    colors=[cmap.get(l, "#58a6ff") for l in labels],
                    autopct="%1.0f%%",
                    textprops={"color": "#c9d1d9", "fontsize": 9},
                    startangle=90)
        ax4.set_title("Alerts by severity", color="#c9d1d9")

        # Panel 5 — RSSI heatmap
        ax5 = fig.add_subplot(gs[1, 2])
        if devs:
            rssi  = [d.signal_rssi or -60 for d in devs]
            d_ids = [d.device_id.replace("dev-", "D") for d in devs]
            im = ax5.imshow([rssi], aspect="auto", cmap="RdYlGn",
                            vmin=-90, vmax=-30, interpolation="nearest")
            ax5.set_xticks(range(len(d_ids)))
            ax5.set_xticklabels(d_ids, rotation=45, fontsize=7)
            ax5.set_yticks([])
            ax5.set_title("RSSI (dBm)", color="#c9d1d9")
            fig.colorbar(im, ax=ax5, fraction=0.046, pad=0.04)

        fig.suptitle(
            f"IoT Network Dashboard — {_now().strftime('%Y-%m-%d %H:%M UTC')}",
            color="#c9d1d9", fontsize=12, y=1.01,
        )
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()

# ─────────────────────────────────────────────────────────────────────────────
# PLOTLY — interactive JSON charts
# ─────────────────────────────────────────────────────────────────────────────

_PLOTLY_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor="#0d1117",
    plot_bgcolor="#161b22",
    font=dict(color="#c9d1d9", size=11),
    margin=dict(l=60, r=20, t=50, b=50),
)
_PLOTLY_COLORS = [
    "#58a6ff","#3fb950","#f78166","#d2a8ff","#ffa657",
    "#79c0ff","#56d364","#ff7b72","#f0883e","#7ee787",
]


def _plotly_timeseries(device_ids: list, metric: str, hours: int = 1) -> dict:
    df  = _load_df(device_ids, hours=hours)
    fig = go.Figure()
    if not df.empty and metric in df.columns:
        for i, did in enumerate(device_ids or df["device_id"].unique()):
            sub = df[df["device_id"] == did].dropna(subset=[metric])
            if sub.empty:
                continue
            fig.add_trace(go.Scatter(
                x=sub["timestamp"], y=sub[metric], name=did, mode="lines",
                line=dict(color=_PLOTLY_COLORS[i % len(_PLOTLY_COLORS)], width=1.5),
            ))
    fig.update_layout(title=f"{metric.replace('_', ' ').title()} — last {hours}h", **_PLOTLY_LAYOUT)
    return json.loads(fig.to_json())


def _plotly_heatmap(hours: int = 1) -> dict:
    df  = _load_df(hours=hours)
    metrics = ["temperature", "humidity", "co2_ppm", "pressure", "noise_db"]
    metrics = [m for m in metrics if m in df.columns] if not df.empty else []
    z, x, y  = [], [], metrics
    if not df.empty and metrics:
        for did in df["device_id"].unique():
            sub = df[df["device_id"] == did]
            x.append(did)
            z.append([round(float(sub[m].mean()), 2) if m in sub.columns else 0 for m in metrics])
    fig = go.Figure(go.Heatmap(z=list(map(list, zip(*z))) if z else [[]], x=x, y=y,
                                colorscale="RdYlGn", showscale=True))
    fig.update_layout(title="Sensor metric heatmap (device × metric mean)", **_PLOTLY_LAYOUT)
    return json.loads(fig.to_json())


def _plotly_alert_histogram(hours: int = 24) -> dict:
    session = Session()
    cutoff  = _now() - timedelta(hours=hours)
    alerts  = session.query(Alert).filter(Alert.triggered_at >= cutoff).all()
    session.close()
    from collections import Counter
    counts = Counter(a.device_id for a in alerts)
    fig = go.Figure(go.Bar(
        x=list(counts.keys()), y=list(counts.values()),
        marker_color="#f78166",
    ))
    fig.update_layout(title=f"Alerts per device — last {hours}h", **_PLOTLY_LAYOUT)
    return json.loads(fig.to_json())


def _plotly_scatter_matrix(hours: int = 1) -> dict:
    df = _load_df(hours=hours)
    cols = [c for c in ["temperature", "humidity", "co2_ppm", "pressure"] if c in (df.columns if not df.empty else [])]
    if not cols or df.empty:
        fig = go.Figure()
        fig.update_layout(title="No data yet", **_PLOTLY_LAYOUT)
        return json.loads(fig.to_json())
    fig = px.scatter_matrix(df, dimensions=cols, color="device_id",
                             color_discrete_sequence=_PLOTLY_COLORS)
    fig.update_layout(title="Scatter matrix (multi-metric)", **_PLOTLY_LAYOUT)
    return json.loads(fig.to_json())

# ─────────────────────────────────────────────────────────────────────────────
# ALERT ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _check_thresholds(session, device_id: str, readings: dict, battery: int, now: datetime):
    all_metrics = {**readings, "battery_percent": float(battery)}
    dedupe_cutoff = now - timedelta(minutes=5)
    for metric, value in all_metrics.items():
        cfg = THRESHOLDS.get(metric)
        if not cfg:
            continue
        direction = cfg["direction"]
        for sev in ("critical", "warning"):
            thresh = cfg[sev]
            triggered = (value > thresh) if direction == "above" else (value < thresh)
            if not triggered:
                continue
            # Deduplicate — skip if same device+metric+severity fired in last 5 min
            exists = session.query(Alert).filter(
                Alert.device_id  == device_id,
                Alert.metric     == metric,
                Alert.severity   == sev,
                Alert.triggered_at >= dedupe_cutoff,
            ).first()
            if exists:
                break
            msg = (f"{device_id}: {metric} = {round(value, 2)} "
                   f"({'above' if direction == 'above' else 'below'} {sev} threshold {thresh})")
            alert = Alert(
                device_id=device_id, metric=metric, value=round(value, 3),
                threshold=thresh, direction=direction,
                severity=sev, message=msg, triggered_at=now,
            )
            session.add(alert)
            socketio.emit("new_alert", {
                "device_id": device_id, "metric": metric,
                "value": round(value, 2), "severity": sev,
                "message": msg, "timestamp": now.isoformat(),
            })
            log.warning("[ALERT] %s", msg)
            break

# ─────────────────────────────────────────────────────────────────────────────
# SIMULATION LOOP
# ─────────────────────────────────────────────────────────────────────────────

SENSOR_FILE = "/tmp/iot_sensor_data.json"   # written by shell script


def _simulation_loop():
    """Background thread: generate sensor readings, persist to DB, push WebSocket events."""
    STATUSES = ["online"] * 6 + ["degraded", "maintenance"]
    tick = 0
    while True:
        tick += 1
        session = Session()
        now     = _now()
        snapshot = []

        # Try to read shell-script output first; fall back to Python generator
        shell_data: dict = {}
        if os.path.exists(SENSOR_FILE):
            try:
                with open(SENSOR_FILE) as f:
                    for item in json.load(f):
                        shell_data[item["device_id"]] = item
            except Exception:
                pass

        for did, dtype, loc, ip, mac in DEVICE_PROFILES:
            shell_dev = shell_data.get(did, {})
            readings  = shell_dev.get("readings") or generate_reading(did, dtype)
            battery   = shell_dev.get("battery_percent") or random.randint(5, 100)
            status    = shell_dev.get("status") or random.choice(STATUSES)
            rssi      = shell_dev.get("signal_rssi") or random.randint(-90, -30)
            uptime    = shell_dev.get("uptime_seconds") or random.randint(100, 864000)
            firmware  = shell_dev.get("firmware_version") or f"2.{random.randint(1,9)}.{random.randint(0,9)}"

            # Upsert Device record
            dev = session.get(Device, did)
            if not dev:
                dev = Device(device_id=did, device_type=dtype, location=loc,
                             ip_address=ip, mac_address=mac, registered_at=now)
                session.add(dev)
            dev.status         = status
            dev.battery        = battery
            dev.signal_rssi    = rssi
            dev.uptime_seconds = uptime
            dev.firmware       = firmware
            dev.last_seen      = now

            # Persist sensor reading
            session.add(SensorReading(device_id=did, timestamp=now, readings=readings))

            # Check alert thresholds
            _check_thresholds(session, did, readings, battery, now)

            # Publish to MQTT — topic matches shell script: iot/sensors/{type}/{id}/telemetry
            mqtt_publish(f"iot/sensors/{dtype}/{did}/telemetry", {
                "device_id": did, "sensor_type": dtype, "timestamp": now.isoformat(),
                "readings": readings, "battery": battery, "status": status,
            })

            snapshot.append({
                "device_id": did, "device_type": dtype, "location": loc,
                "status": status, "battery_percent": battery,
                "signal_rssi": rssi, "firmware_version": firmware,
                "uptime_seconds": uptime, "ip_address": ip, "mac_address": mac,
                "timestamp": now.isoformat(), "readings": readings,
            })

        try:
            session.commit()
        except Exception as e:
            log.error("[DB] commit error: %s", e)
            session.rollback()
        finally:
            session.close()

        # Push full snapshot via WebSocket
        socketio.emit("devices_update", {"devices": snapshot, "tick": tick, "timestamp": now.isoformat()})
        log.info("[Tick %04d] %d devices | %d alerts", tick, len(snapshot),
                 sum(1 for d in snapshot if d["status"] != "online"))

        time.sleep(int(os.getenv("SENSOR_INTERVAL", "3")))

# ─────────────────────────────────────────────────────────────────────────────
# REST API — Flask routes
# ─────────────────────────────────────────────────────────────────────────────

@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    return response


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    session = Session()
    dev_cnt = session.query(Device).count()
    session.close()
    return jsonify({
        "status":    "ok",
        "timestamp": _now().isoformat(),
        "devices":   dev_cnt,
        "mqtt":      _mqtt_connected,
        "scapy":     SCAPY_AVAILABLE,
    })


# ── Device management ─────────────────────────────────────────────────────────

@app.route("/api/devices", methods=["GET"])
def list_devices():
    session = Session()
    devs = session.query(Device).all()
    result = [{
        "device_id": d.device_id, "device_type": d.device_type,
        "location":  d.location,  "status":      d.status,
        "battery_percent": d.battery, "signal_rssi": d.signal_rssi,
        "firmware":   d.firmware,  "uptime_seconds": d.uptime_seconds,
        "ip_address": d.ip_address, "mac_address":  d.mac_address,
        "last_seen":  d.last_seen.isoformat()  if d.last_seen  else None,
        "registered_at": d.registered_at.isoformat() if d.registered_at else None,
        "alerts_enabled": d.alerts_enabled,
    } for d in devs]
    session.close()
    return jsonify(result)


@app.route("/api/devices/<device_id>", methods=["GET"])
def get_device(device_id):
    session = Session()
    dev = session.get(Device, device_id)
    if not dev:
        session.close()
        abort(404, f"Device {device_id} not found")
    result = {
        "device_id": dev.device_id, "device_type": dev.device_type,
        "location": dev.location,   "status": dev.status,
        "battery_percent": dev.battery, "signal_rssi": dev.signal_rssi,
        "firmware": dev.firmware, "uptime_seconds": dev.uptime_seconds,
        "ip_address": dev.ip_address, "mac_address": dev.mac_address,
        "last_seen": dev.last_seen.isoformat() if dev.last_seen else None,
        "alerts_enabled": dev.alerts_enabled,
    }
    session.close()
    return jsonify(result)


@app.route("/api/devices/<device_id>/readings", methods=["GET"])
def device_readings(device_id):
    session = Session()
    limit   = int(request.args.get("limit", 50))
    rows = (
        session.query(SensorReading)
               .filter(SensorReading.device_id == device_id)
               .order_by(SensorReading.timestamp.desc())
               .limit(limit).all()
    )
    result = [{"timestamp": r.timestamp.isoformat(), "readings": r.readings} for r in rows]
    session.close()
    return jsonify(result)


@app.route("/api/devices/<device_id>/command", methods=["POST"])
def device_command(device_id):
    data = request.get_json()
    if not data or "command" not in data:
        abort(400, "Missing 'command' field")
    session = Session()
    cmd = Command(device_id=device_id, command=data["command"],
                  payload=data.get("payload", {}), status="pending", created_at=_now())
    session.add(cmd)
    try:
        session.commit()
        cmd_id = cmd.id
    except Exception as e:
        session.rollback()
        session.close()
        abort(500, str(e))
    session.close()

    # Simulate async execution
    def _execute(cid):
        time.sleep(1.5)
        s = Session()
        c = s.get(Command, cid)
        if c:
            c.status      = "executed"
            c.executed_at = _now()
            s.commit()
            socketio.emit("command_executed", {
                "device_id": device_id, "command": c.command, "command_id": cid
            })
        s.close()

    threading.Thread(target=_execute, args=(cmd_id,), daemon=True).start()
    return jsonify({"command_id": cmd_id, "status": "pending"})


@app.route("/api/devices/<device_id>/alerts_toggle", methods=["POST"])
def toggle_alerts(device_id):
    session = Session()
    dev = session.get(Device, device_id)
    if not dev:
        session.close()
        abort(404)
    dev.alerts_enabled = not dev.alerts_enabled
    session.commit()
    result = {"device_id": device_id, "alerts_enabled": dev.alerts_enabled}
    session.close()
    return jsonify(result)


# ── Alert management ──────────────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    session = Session()
    limit   = int(request.args.get("limit", 100))
    unack   = request.args.get("unacknowledged") == "true"
    q = session.query(Alert).order_by(Alert.triggered_at.desc())
    if unack:
        q = q.filter(Alert.acknowledged == False)
    alerts = q.limit(limit).all()
    result = [{
        "id": a.id, "device_id": a.device_id, "metric": a.metric,
        "value": a.value, "threshold": a.threshold, "direction": a.direction,
        "severity": a.severity, "message": a.message,
        "triggered_at": a.triggered_at.isoformat(),
        "acknowledged": a.acknowledged,
    } for a in alerts]
    session.close()
    return jsonify(result)


@app.route("/api/alerts/<int:alert_id>/acknowledge", methods=["POST"])
def ack_alert(alert_id):
    session = Session()
    alert   = session.get(Alert, alert_id)
    if not alert:
        session.close()
        abort(404)
    alert.acknowledged = True
    session.commit()
    session.close()
    return jsonify({"id": alert_id, "acknowledged": True})


@app.route("/api/alerts/acknowledge_all", methods=["POST"])
def ack_all_alerts():
    session = Session()
    session.query(Alert).filter(Alert.acknowledged == False).update({"acknowledged": True})
    session.commit()
    session.close()
    return jsonify({"acknowledged": True})


# ── Threshold management ──────────────────────────────────────────────────────

@app.route("/api/thresholds", methods=["GET", "POST"])
def thresholds():
    if request.method == "POST":
        data = request.get_json()
        if data:
            THRESHOLDS.update(data)
    return jsonify(THRESHOLDS)


# ── Pandas analytics ──────────────────────────────────────────────────────────

@app.route("/api/analytics/summary")
def analytics_summary():
    session = Session()
    devs    = session.query(Device).all()
    online  = sum(1 for d in devs if d.status == "online")
    degraded = sum(1 for d in devs if d.status == "degraded")
    maintenance = sum(1 for d in devs if d.status == "maintenance")
    low_batt  = sum(1 for d in devs if d.battery and d.battery < 20)
    unack     = session.query(Alert).filter(Alert.acknowledged == False).count()
    crit      = session.query(Alert).filter(Alert.acknowledged == False, Alert.severity == "critical").count()
    session.close()
    df = _load_df(hours=int(request.args.get("hours", 1)))
    return jsonify({
        "total_devices": len(devs), "online": online,
        "degraded": degraded, "maintenance": maintenance,
        "low_battery_devices": low_batt,
        "unacknowledged_alerts": unack, "critical_alerts": crit,
        "readings_last_hour": len(df),
        "pandas_stats":  _pd_summary(df),
    })


@app.route("/api/analytics/timeseries/<device_id>")
def timeseries(device_id):
    hours  = int(request.args.get("hours",  1))
    metric = request.args.get("metric", "temperature")
    df     = _load_df([device_id], hours=hours)
    if df.empty or metric not in df.columns:
        return jsonify({"device_id": device_id, "metric": metric, "data": []})
    sub = df[["timestamp", metric]].dropna()
    return jsonify({
        "device_id": device_id, "metric": metric,
        "data": [{"timestamp": str(r["timestamp"]), "value": r[metric]}
                 for _, r in sub.iterrows()],
    })


@app.route("/api/analytics/devices_summary")
def devices_summary():
    return jsonify(_pd_device_summary(_load_df(hours=int(request.args.get("hours", 1)))))


@app.route("/api/analytics/correlation")
def correlation():
    return jsonify(_pd_correlation(_load_df(hours=int(request.args.get("hours", 1)))))


@app.route("/api/analytics/anomalies")
def anomalies():
    df = _load_df(hours=int(request.args.get("hours", 1)))
    return jsonify(_pd_anomalies(df, float(request.args.get("z", 2.5))))


@app.route("/api/analytics/rolling/<device_id>")
def rolling(device_id):
    df = _load_df([device_id], hours=int(request.args.get("hours", 1)))
    return jsonify(_pd_rolling(df, request.args.get("metric", "temperature"),
                               int(request.args.get("window", 5))))


# ── Matplotlib endpoints ──────────────────────────────────────────────────────

@app.route("/api/charts/timeseries.png")
def chart_ts_png():
    metric = request.args.get("metric", "temperature")
    hours  = int(request.args.get("hours", 1))
    devs   = request.args.getlist("device") or [p[0] for p in DEVICE_PROFILES[:5]]
    return send_file(io.BytesIO(_mpl_timeseries(devs, metric, hours)),
                     mimetype="image/png", download_name=f"{metric}.png")


@app.route("/api/charts/dashboard.png")
def chart_dashboard_png():
    return send_file(io.BytesIO(_mpl_dashboard(int(request.args.get("hours", 1)))),
                     mimetype="image/png", download_name="dashboard.png")


@app.route("/api/charts/timeseries_b64")
def chart_ts_b64():
    metric = request.args.get("metric", "temperature")
    hours  = int(request.args.get("hours", 1))
    devs   = request.args.getlist("device") or [p[0] for p in DEVICE_PROFILES[:5]]
    png    = _mpl_timeseries(devs, metric, hours)
    return jsonify({"image": "data:image/png;base64," + base64.b64encode(png).decode()})


@app.route("/api/charts/dashboard_b64")
def chart_dashboard_b64():
    png = _mpl_dashboard(int(request.args.get("hours", 1)))
    return jsonify({"image": "data:image/png;base64," + base64.b64encode(png).decode()})


# ── Plotly endpoints ──────────────────────────────────────────────────────────

@app.route("/api/plotly/timeseries")
def plotly_ts():
    metric = request.args.get("metric", "temperature")
    hours  = int(request.args.get("hours", 1))
    devs   = request.args.getlist("device") or [p[0] for p in DEVICE_PROFILES]
    return jsonify(_plotly_timeseries(devs, metric, hours))


@app.route("/api/plotly/heatmap")
def plotly_heatmap_route():
    return jsonify(_plotly_heatmap(int(request.args.get("hours", 1))))


@app.route("/api/plotly/alerts_histogram")
def plotly_alerts():
    return jsonify(_plotly_alert_histogram(int(request.args.get("hours", 24))))


@app.route("/api/plotly/scatter_matrix")
def plotly_scatter():
    return jsonify(_plotly_scatter_matrix(int(request.args.get("hours", 1))))


# ── MQTT endpoints ────────────────────────────────────────────────────────────

@app.route("/api/mqtt/status")
def mqtt_status():
    return jsonify({
        "connected": _mqtt_connected,
        "broker":    MQTT_BROKER,
        "port":      MQTT_PORT,
        "enabled":   MQTT_ENABLED,
    })


@app.route("/api/mqtt/publish", methods=["POST"])
def mqtt_publish_api():
    data = request.get_json()
    if not data or "topic" not in data:
        abort(400, "Missing 'topic'")
    ok = mqtt_publish(data["topic"], data.get("payload", "{}"))
    return jsonify({"success": ok, "topic": data["topic"]})


@app.route("/api/mqtt/messages")
def mqtt_messages():
    session   = Session()
    limit     = int(request.args.get("limit", 50))
    direction = request.args.get("direction")
    q = session.query(MqttMessage).order_by(MqttMessage.received_at.desc())
    if direction:
        q = q.filter(MqttMessage.direction == direction)
    out = [{
        "id": m.id, "topic": m.topic, "payload": m.payload,
        "direction": m.direction, "received_at": m.received_at.isoformat(),
    } for m in q.limit(limit)]
    session.close()
    return jsonify(out)


# ── Scapy endpoints ───────────────────────────────────────────────────────────

@app.route("/api/scapy/status")
def scapy_status():
    return jsonify({
        "available":        SCAPY_AVAILABLE,
        "captured_packets": len(_captured_packets),
        "total_in_db":      Session().query(PacketLog).count(),
    })


@app.route("/api/scapy/packets")
def scapy_packets():
    limit = int(request.args.get("limit", 50))
    return jsonify(_captured_packets[-limit:])


@app.route("/api/scapy/stats")
def scapy_stats():
    return jsonify(_analyze_packets(_captured_packets))


@app.route("/api/scapy/craft", methods=["POST"])
def scapy_craft():
    data  = request.get_json() or {}
    ptype = data.get("type", "mqtt")
    src   = data.get("src_ip", "192.168.1.101")
    dst   = data.get("dst_ip", "192.168.1.200")
    if ptype == "mqtt":
        return jsonify(_craft_mqtt_packet(src, dst, data.get("payload", "{}")))
    return jsonify(_craft_http_packet(src, dst, data.get("path", "/api/devices")))


@app.route("/api/scapy/db_packets")
def scapy_db_packets():
    session = Session()
    limit   = int(request.args.get("limit", 100))
    rows    = session.query(PacketLog).order_by(PacketLog.captured_at.desc()).limit(limit).all()
    out = [{
        "id": r.id, "src_ip": r.src_ip, "dst_ip": r.dst_ip,
        "protocol": r.protocol, "src_port": r.src_port, "dst_port": r.dst_port,
        "length": r.length, "summary": r.summary,
        "captured_at": r.captured_at.isoformat(),
    } for r in rows]
    session.close()
    return jsonify(out)


# ── WebSocket events ──────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    log.info("[WS] Client connected: %s", request.sid)
    emit("connected", {"message": "IoT Manager WebSocket ready", "sid": request.sid})


@socketio.on("disconnect")
def on_disconnect():
    log.info("[WS] Client disconnected: %s", request.sid)


@socketio.on("subscribe_device")
def on_subscribe(data):
    emit("subscribed", {"device_id": data.get("device_id")})


@socketio.on("publish_mqtt")
def on_ws_publish(data):
    ok = mqtt_publish(data.get("topic", "iot/test"), data.get("payload", "{}"))
    emit("mqtt_publish_result", {"success": ok, "topic": data.get("topic")})


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  IoT Device Manager — Full Stack Backend")
    log.info("  Libraries : Flask · paho-mqtt · SQLAlchemy · Pandas")
    log.info("              Matplotlib · Plotly · Scapy")
    log.info("  Protocols : MQTT · HTTP/REST · WebSocket (Socket.IO)")
    log.info("  Database  : %s", DATABASE_URL)
    log.info("  Scapy     : %s", "available" if SCAPY_AVAILABLE else "craft-only")
    log.info("=" * 60)

    _init_mqtt()
    threading.Thread(target=_simulation_loop,        daemon=True).start()
    threading.Thread(target=_packet_simulation_loop, daemon=True).start()

    log.info("[HTTP] REST API  → http://0.0.0.0:5000/api/")
    log.info("[WS]   Socket.IO → ws://0.0.0.0:5000/socket.io/")

    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=False,
        use_reloader=False,
        log_output=False,
        allow_unsafe_werkzeug=True
    )
