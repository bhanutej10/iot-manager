#!/usr/bin/env python3
"""
MQTT Subscriber — mqtt_subscriber.py
Connects to the Mosquitto broker and prints live telemetry
from all 10 IoT devices to the terminal.

Usage:
    python mqtt_subscriber.py
    python mqtt_subscriber.py --broker localhost --port 1883
"""

import argparse
import json
import sys
from datetime import datetime

import paho.mqtt.client as mqtt

# ── ANSI colours ─────────────────────────────────────────────
C = {
    "reset":  "\033[0m",  "bold":   "\033[1m",  "dim":    "\033[2m",
    "cyan":   "\033[36m", "green":  "\033[32m",  "yellow": "\033[33m",
    "red":    "\033[31m", "purple": "\033[35m",  "teal":   "\033[96m",
}

THRESHOLDS = {
    "temperature":     {"warning": 35.0,  "critical": 45.0,  "direction": "above"},
    "co2_ppm":         {"warning": 1000.0,"critical": 2000.0,"direction": "above"},
    "humidity":        {"warning": 85.0,  "critical": 95.0,  "direction": "above"},
    "battery_percent": {"warning": 20.0,  "critical": 10.0,  "direction": "below"},
    "pm25":            {"warning": 75.0,  "critical": 150.0, "direction": "above"},
    "vibration_z":     {"warning": 7.0,   "critical": 9.0,   "direction": "above"},
}

def alert_level(metric: str, value) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    cfg = THRESHOLDS.get(metric)
    if not cfg:
        return None
    if cfg["direction"] == "above":
        if value > cfg["critical"]: return "CRITICAL"
        if value > cfg["warning"]:  return "WARNING"
    else:
        if value < cfg["critical"]: return "CRITICAL"
        if value < cfg["warning"]:  return "WARNING"
    return None


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        client.subscribe("iot/#", qos=1)
        print(f"\n{C['bold']}{C['teal']}[MQTT]{C['reset']} Connected — subscribed to iot/#\n")
    else:
        print(f"{C['red']}[MQTT] Connection failed: {reason_code}{C['reset']}")
        sys.exit(1)


def on_message(client, userdata, msg):
    ts  = datetime.now().strftime("%H:%M:%S")
    raw = msg.payload.decode("utf-8", errors="replace")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"{C['dim']}{ts}{C['reset']} {C['cyan']}{msg.topic}{C['reset']}  {raw[:120]}")
        return

    # Handle array (full snapshot) or single device object
    devices = data if isinstance(data, list) else [data]
    for dev in devices:
        if not isinstance(dev, dict):
            continue

        did      = dev.get("device_id", msg.topic.split("/")[2] if "/" in msg.topic else "?")
        status   = dev.get("status", "unknown")
        battery  = dev.get("battery_percent", "?")
        readings = dev.get("readings", {})

        # Status colour
        s_col = C["green"] if status == "online" else C["yellow"] if status == "degraded" else C["red"]
        # Battery colour
        b_col = C["red"] if isinstance(battery, int) and battery < 10 else \
                C["yellow"] if isinstance(battery, int) and battery < 20 else C["reset"]

        print(f"{C['dim']}{ts}{C['reset']}  "
              f"{C['cyan']}{did:<10}{C['reset']}  "
              f"{s_col}{status:<12}{C['reset']}  "
              f"{b_col}🔋{battery:>3}%{C['reset']}", end="")

        alerts_fired = []
        for i, (k, v) in enumerate(readings.items()):
            if i >= 4:
                print(f"  {C['dim']}+{len(readings)-4} more{C['reset']}", end="")
                break
            val_str  = f"{v:.1f}" if isinstance(v, float) else str(v)
            level    = alert_level(k, v)
            col      = C["red"] if level == "CRITICAL" else C["yellow"] if level == "WARNING" else ""
            key_short = k[:9]
            print(f"  {C['dim']}{key_short}:{C['reset']}{col}{val_str}{C['reset']}", end="")
            if level:
                alerts_fired.append(f"{k}={val_str} [{level}]")

        print()

        if alerts_fired:
            for a in alerts_fired:
                print(f"  {C['red']}  ⚠  ALERT: {a}{C['reset']}")


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    print(f"\n{C['yellow']}[MQTT] Disconnected (code: {reason_code}) — reconnecting…{C['reset']}")


def main():
    parser = argparse.ArgumentParser(description="IoT MQTT Subscriber")
    parser.add_argument("--broker", default="localhost", help="MQTT broker host")
    parser.add_argument("--port",   type=int, default=1883, help="MQTT broker port")
    args = parser.parse_args()

    print(f"\n{C['bold']}IoT MQTT Subscriber{C['reset']}")
    print(f"Connecting to {args.broker}:{args.port} …")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect

    try:
        client.connect(args.broker, args.port, keepalive=60)
        client.loop_forever()
    except KeyboardInterrupt:
        print(f"\n{C['dim']}Disconnecting…{C['reset']}")
        client.disconnect()
    except ConnectionRefusedError:
        print(f"{C['red']}Cannot connect to {args.broker}:{args.port}{C['reset']}")
        print("Is Mosquitto running?  →  docker compose up mosquitto")
        sys.exit(1)


if __name__ == "__main__":
    main()
