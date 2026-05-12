#!/bin/bash
# =============================================================
# IoT Sensor Simulator — simulate_sensors.sh
# 10 dedicated environmental / physical sensors:
#   Mandatory : temperature, pressure, humidity, moisture
#   Additional: wind_speed, rainfall, light_intensity,
#               co2, noise_level, soil_ph
# Values drift realistically every SENSOR_INTERVAL seconds
# =============================================================

DEVICE_COUNT=10
OUTPUT_FILE="/tmp/iot_sensor_data.json"
INTERVAL=${SENSOR_INTERVAL:-3}
MQTT_BROKER=${MQTT_BROKER:-"mosquitto"}
MQTT_PORT=${MQTT_PORT:-1883}
USE_MQTT=${USE_MQTT:-"false"}

# ─────────────────────────────────────────────────────────────
# 10 Sensor Device Profiles: id | sensor_type | location
# ─────────────────────────────────────────────────────────────
DEVICES=(
  "dev-001|temperature|server_room"
  "dev-002|pressure|boiler_room"
  "dev-003|humidity|greenhouse"
  "dev-004|moisture|agricultural_field"
  "dev-005|wind_speed|rooftop"
  "dev-006|rainfall|weather_station"
  "dev-007|light_intensity|solar_farm"
  "dev-008|co2|factory_floor"
  "dev-009|noise_level|industrial_zone"
  "dev-010|soil_ph|greenhouse_b"
)

# ─────────────────────────────────────────────────────────────
# Utility: random float between min and max (2 decimal places)
# ─────────────────────────────────────────────────────────────
rand_float() {
    local min=$1
    local max=$2
    awk -v min="$min" -v max="$max" -v seed="$RANDOM$RANDOM" \
        'BEGIN { srand(seed); printf "%.2f", min + rand() * (max - min) }'
}

# ─────────────────────────────────────────────────────────────
# Utility: random integer between min and max
# ─────────────────────────────────────────────────────────────
rand_int() {
    echo $(( RANDOM % ($2 - $1 + 1) + $1 ))
}

# ─────────────────────────────────────────────────────────────
# Utility: pick random device status (weighted toward online)
# ─────────────────────────────────────────────────────────────
rand_status() {
    local pool=("online" "online" "online" "online" "online" "online" "degraded" "maintenance")
    echo "${pool[$RANDOM % ${#pool[@]}]}"
}

# ─────────────────────────────────────────────────────────────
# Generate sensor readings — each sensor has one primary
# measurement plus related secondary metrics
# ─────────────────────────────────────────────────────────────
generate_readings() {
    local type=$1

    case $type in

        # ── SENSOR 1: Temperature ──────────────────────────────
        # Primary   : temperature_celsius
        # Secondary : fahrenheit (derived), heat_index, dew_point,
        #             rate_of_change
        temperature)
            local c=$(rand_float 15.0 75.0)
            local f=$(awk -v c="$c" 'BEGIN { printf "%.2f", c * 9/5 + 32 }')
            local hi=$(rand_float 18.0 80.0)
            local dp=$(rand_float 5.0 30.0)
            local roc=$(rand_float -0.5 0.5)
            echo "\"temperature_celsius\":${c},\"temperature_fahrenheit\":${f},\"heat_index_celsius\":${hi},\"dew_point_celsius\":${dp},\"rate_of_change_c_per_min\":${roc}"
            ;;

        # ── SENSOR 2: Pressure ────────────────────────────────
        # Primary   : pressure_hpa (atmospheric)
        # Secondary : pressure_psi, pressure_bar, altitude_m,
        #             pressure_trend
        pressure)
            local hpa=$(rand_float 960.0 1050.0)
            local psi=$(awk -v h="$hpa" 'BEGIN { printf "%.3f", h * 0.0145038 }')
            local bar=$(awk -v h="$hpa" 'BEGIN { printf "%.4f", h / 1000.0 }')
            local alt=$(awk -v h="$hpa" 'BEGIN { printf "%.1f", (1-(h/1013.25)^0.190284)*44307.7 }')
            local trd=$(rand_float -2.0 2.0)
            echo "\"pressure_hpa\":${hpa},\"pressure_psi\":${psi},\"pressure_bar\":${bar},\"altitude_m\":${alt},\"pressure_trend_hpa_per_hr\":${trd}"
            ;;

        # ── SENSOR 3: Humidity ───────────────────────────────
        # Primary   : relative_humidity_percent
        # Secondary : absolute_humidity, specific_humidity,
        #             vapor_pressure, comfort_index
        humidity)
            local rh=$(rand_float 20.0 99.0)
            local ah=$(awk -v r="$rh" 'BEGIN { printf "%.2f", r * 0.217 }')
            local sh=$(awk -v r="$rh" 'BEGIN { printf "%.4f", r * 0.00622 }')
            local vp=$(rand_float 0.5 4.2)
            local ci=$(rand_float 0.0 100.0)
            echo "\"relative_humidity_percent\":${rh},\"absolute_humidity_g_per_m3\":${ah},\"specific_humidity_kg_per_kg\":${sh},\"vapor_pressure_kpa\":${vp},\"comfort_index\":${ci}"
            ;;

        # ── SENSOR 4: Moisture ───────────────────────────────
        # Primary   : soil_moisture_percent (volumetric water content)
        # Secondary : moisture_raw_adc, water_retention_percent,
        #             field_capacity, wilting_point
        moisture)
            local vwc=$(rand_float 5.0 60.0)
            local raw=$(rand_int 200 900)
            local ret=$(rand_float 10.0 85.0)
            local fcp=$(rand_float 25.0 45.0)
            local wlp=$(rand_float 5.0 15.0)
            echo "\"soil_moisture_percent\":${vwc},\"moisture_raw_adc\":${raw},\"water_retention_percent\":${ret},\"field_capacity_percent\":${fcp},\"wilting_point_percent\":${wlp}"
            ;;

        # ── SENSOR 5: Wind Speed ─────────────────────────────
        # Primary   : wind_speed_kmh
        # Secondary : wind_speed_ms, wind_direction_deg,
        #             gust_speed_kmh, beaufort_scale
        wind_speed)
            local kmh=$(rand_float 0.0 120.0)
            local ms=$(awk  -v k="$kmh" 'BEGIN { printf "%.2f", k / 3.6 }')
            local dir=$(rand_int 0 359)
            local gust=$(awk -v k="$kmh" 'BEGIN { printf "%.2f", k * 1.3 }')
            local bft=$(awk -v k="$kmh" 'BEGIN {
                if      (k<1)   b=0
                else if (k<6)   b=1
                else if (k<12)  b=2
                else if (k<20)  b=3
                else if (k<29)  b=4
                else if (k<39)  b=5
                else if (k<50)  b=6
                else if (k<62)  b=7
                else if (k<75)  b=8
                else if (k<89)  b=9
                else if (k<103) b=10
                else if (k<118) b=11
                else            b=12
                printf "%d", b }')
            echo "\"wind_speed_kmh\":${kmh},\"wind_speed_ms\":${ms},\"wind_direction_deg\":${dir},\"gust_speed_kmh\":${gust},\"beaufort_scale\":${bft}"
            ;;

        # ── SENSOR 6: Rainfall ───────────────────────────────
        # Primary   : rainfall_mm (current accumulation)
        # Secondary : rainfall_rate_mm_per_hr, daily_total_mm,
        #             weekly_total_mm, intensity_level
        rainfall)
            local cur=$(rand_float 0.0 50.0)
            local rate=$(rand_float 0.0 100.0)
            local daily=$(rand_float 0.0 200.0)
            local weekly=$(rand_float 0.0 500.0)
            local level
            if   awk -v r="$rate" 'BEGIN{exit !(r<2.5)}';  then level="light"
            elif awk -v r="$rate" 'BEGIN{exit !(r<10)}';   then level="moderate"
            elif awk -v r="$rate" 'BEGIN{exit !(r<50)}';   then level="heavy"
            else level="violent"
            fi
            echo "\"rainfall_mm\":${cur},\"rainfall_rate_mm_per_hr\":${rate},\"daily_total_mm\":${daily},\"weekly_total_mm\":${weekly},\"intensity_level\":\"${level}\""
            ;;

        # ── SENSOR 7: Light Intensity ────────────────────────
        # Primary   : light_lux (illuminance)
        # Secondary : uv_index, par_umol_m2_s, solar_radiation_w_m2,
        #             daylight_hours
        light_intensity)
            local lux=$(rand_float 0.0 120000.0)
            local uv=$(rand_float 0.0 11.0)
            local par=$(awk  -v l="$lux" 'BEGIN { printf "%.1f", l * 0.0185 }')
            local rad=$(awk  -v l="$lux" 'BEGIN { printf "%.1f", l / 120.0  }')
            local dh=$(rand_float 0.0 16.0)
            echo "\"light_lux\":${lux},\"uv_index\":${uv},\"par_umol_m2_s\":${par},\"solar_radiation_w_m2\":${rad},\"daylight_hours\":${dh}"
            ;;

        # ── SENSOR 8: CO2 ────────────────────────────────────
        # Primary   : co2_ppm (carbon dioxide concentration)
        # Secondary : co_ppm, voc_ppb, pm25_ug_m3, air_quality_index
        co2)
            local co2=$(rand_float 350.0 5000.0)
            local co=$(rand_float 0.0 100.0)
            local voc=$(rand_float 0.0 2000.0)
            local pm=$(rand_float 0.0 300.0)
            local aqi
            if   awk -v c="$co2" 'BEGIN{exit !(c<600)}';  then aqi="good"
            elif awk -v c="$co2" 'BEGIN{exit !(c<1000)}'; then aqi="moderate"
            elif awk -v c="$co2" 'BEGIN{exit !(c<2000)}'; then aqi="poor"
            else aqi="hazardous"
            fi
            echo "\"co2_ppm\":${co2},\"co_ppm\":${co},\"voc_ppb\":${voc},\"pm25_ug_m3\":${pm},\"air_quality_index\":\"${aqi}\""
            ;;

        # ── SENSOR 9: Noise Level ────────────────────────────
        # Primary   : noise_db (average sound pressure level)
        # Secondary : peak_db, noise_frequency_hz, leq_db, noise_class
        noise_level)
            local avg=$(rand_float 20.0 120.0)
            local peak=$(awk -v a="$avg" 'BEGIN { printf "%.1f", a * 1.15 }')
            local freq=$(rand_int 20 20000)
            local leq=$(awk -v a="$avg" 'BEGIN { srand(); printf "%.1f", a - 2 + rand()*4 }')
            local cls
            if   awk -v d="$avg" 'BEGIN{exit !(d<40)}'; then cls="quiet"
            elif awk -v d="$avg" 'BEGIN{exit !(d<70)}'; then cls="moderate"
            elif awk -v d="$avg" 'BEGIN{exit !(d<90)}'; then cls="loud"
            else cls="hazardous"
            fi
            echo "\"noise_db\":${avg},\"peak_db\":${peak},\"noise_frequency_hz\":${freq},\"leq_db\":${leq},\"noise_class\":\"${cls}\""
            ;;

        # ── SENSOR 10: Soil pH ───────────────────────────────
        # Primary   : soil_ph (acidity / alkalinity)
        # Secondary : soil_temperature_celsius, electrical_conductivity,
        #             organic_matter_percent, fertility_index, ph_status
        soil_ph)
            local ph=$(rand_float 4.0 9.0)
            local tmp=$(rand_float 5.0 40.0)
            local ec=$(rand_float 0.1 4.0)
            local om=$(rand_float 0.5 8.0)
            local fi=$(rand_float 0.0 100.0)
            local st
            if   awk -v p="$ph" 'BEGIN{exit !(p<5.5)}'; then st="very_acidic"
            elif awk -v p="$ph" 'BEGIN{exit !(p<6.5)}'; then st="acidic"
            elif awk -v p="$ph" 'BEGIN{exit !(p<7.5)}'; then st="neutral"
            elif awk -v p="$ph" 'BEGIN{exit !(p<8.5)}'; then st="alkaline"
            else st="very_alkaline"
            fi
            echo "\"soil_ph\":${ph},\"soil_temperature_celsius\":${tmp},\"electrical_conductivity_ms_cm\":${ec},\"organic_matter_percent\":${om},\"fertility_index\":${fi},\"ph_status\":\"${st}\""
            ;;

    esac
}

# ─────────────────────────────────────────────────────────────
# Build complete JSON payload for one device
# ─────────────────────────────────────────────────────────────
build_payload() {
    local entry=$1
    local id=$(echo "$entry"   | cut -d'|' -f1)
    local type=$(echo "$entry" | cut -d'|' -f2)
    local loc=$(echo "$entry"  | cut -d'|' -f3)
    local ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local status=$(rand_status)
    local battery=$(rand_int 5 100)
    local rssi=$(rand_int -90 -30)
    local uptime=$(rand_int 100 864000)
    local node=$(echo "$id" | sed 's/dev-//')
    local fw_maj=$(rand_int 1 9)
    local fw_min=$(rand_int 0 9)
    local readings=$(generate_readings "$type")

    printf '{
    "device_id": "%s",
    "sensor_type": "%s",
    "location": "%s",
    "status": "%s",
    "battery_percent": %d,
    "signal_rssi": %d,
    "firmware_version": "2.%d.%d",
    "uptime_seconds": %d,
    "ip_address": "192.168.1.%s",
    "timestamp": "%s",
    "readings": { %s }
  }' \
        "$id" "$type" "$loc" "$status" \
        "$battery" "$rssi" \
        "$fw_maj" "$fw_min" \
        "$uptime" "$node" \
        "$ts" \
        "$readings"
}

# ─────────────────────────────────────────────────────────────
# Main loop — infinite, updates every INTERVAL seconds
# ─────────────────────────────────────────────────────────────
echo "[IoT Simulator] ============================================"
echo "[IoT Simulator]  10-Sensor IoT Network"
echo "[IoT Simulator]  MANDATORY : temperature | pressure"
echo "[IoT Simulator]              humidity    | moisture"
echo "[IoT Simulator]  ADDITIONAL: wind_speed  | rainfall"
echo "[IoT Simulator]              light_intensity | co2"
echo "[IoT Simulator]              noise_level | soil_ph"
echo "[IoT Simulator]  Interval  : ${INTERVAL}s"
echo "[IoT Simulator]  MQTT      : ${MQTT_BROKER}:${MQTT_PORT} (${USE_MQTT})"
echo "[IoT Simulator]  Output    : ${OUTPUT_FILE}"
echo "[IoT Simulator] ============================================"

TICK=0
while true; do
    TICK=$((TICK + 1))
    SNAPSHOT="["
    FIRST=true

    for device_entry in "${DEVICES[@]}"; do
        [ "$FIRST" = true ] && FIRST=false || SNAPSHOT+=","
        SNAPSHOT+=$(build_payload "$device_entry")

        # Publish each sensor to its own MQTT topic
        if [ "$USE_MQTT" = "true" ] && command -v mosquitto_pub &>/dev/null; then
            dev_id=$(echo "$device_entry"  | cut -d'|' -f1)
            sen_type=$(echo "$device_entry" | cut -d'|' -f2)
            payload=$(build_payload "$device_entry")
            mosquitto_pub \
                -h "$MQTT_BROKER" -p "$MQTT_PORT" \
                -t "iot/sensors/${sen_type}/${dev_id}/telemetry" \
                -m "$payload" -q 1 2>/dev/null &
        fi
    done

    SNAPSHOT+="]"
    echo "$SNAPSHOT" > "$OUTPUT_FILE"

    BYTES=$(printf '%s' "$SNAPSHOT" | wc -c)
    echo "[$(date '+%H:%M:%S')] Tick #${TICK} — ${DEVICE_COUNT} sensors — ${BYTES} bytes"
    sleep "$INTERVAL"
done
