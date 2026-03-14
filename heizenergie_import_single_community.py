#!/usr/bin/env python3
"""
Heating Energy Import for Home Assistant
=========================================
Distributes the monthly heating consumption (provided by your landlord/utility)
weighted across all days of the month, based on Tado thermostat valve positions.

Days where the central heating was clearly off (outdoor temperature above threshold
AND all Tado zones at 0%) receive a weight of 0.

Falls back to equal distribution if no Tado data is available.

Called via shell_command:
  python3 /config/heizenergie_import_single.py "2026-02-01 00:00:00" "513.43"

Requirements:
  - Home Assistant with MariaDB (core-mariadb add-on)
  - Tado integration with heating percentage sensors
  - pymysql: pip3 install pymysql --target /config/deps
"""

import sys
sys.path.insert(0, "/config/deps")
import pymysql
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import calendar

# ── Configuration ─────────────────────────────────────────────────────────────
# Database connection (MariaDB add-on defaults)
DB_HOST = "core-mariadb"
DB_USER = "homeassistant"
DB_PASS = "YOUR_DB_PASSWORD"   # ← change this
DB_NAME = "homeassistant"

# Home Assistant statistic ID for actual consumption
# Must match the entity_id of your template sensor in configuration.yaml
STATISTIC_ID = "sensor.heating_energy_kwh"

UNIT     = "kWh"
SOURCE   = "recorder"
LOCAL_TZ = ZoneInfo("Europe/Berlin")   # ← change to your timezone

# Tado heating percentage sensors (one per zone)
# Replace with your actual entity IDs from Tado integration
TADO_SENSORS = [
    "sensor.living_room_heating",
    "sensor.kitchen_heating",
    "sensor.bathroom_heating",
    "sensor.office_heating",
    "sensor.bedroom_heating",
]

# Outdoor temperature sensor (used for summer detection)
OUTDOOR_TEMP_SENSOR = "sensor.outdoor_temperature"

# Days with average outdoor temp >= this value AND Tado = 0% → no central heating
HEATING_CUTOFF_TEMP = 15.0   # °C
# ──────────────────────────────────────────────────────────────────────────────


def get_day_ts(year, month, day):
    dt_local = datetime(year, month, day, 0, 0, 0, tzinfo=LOCAL_TZ)
    return dt_local.astimezone(timezone.utc).timestamp()


def get_or_create_metadata(cursor, statistic_id, unit):
    cursor.execute(
        "SELECT id FROM statistics_meta WHERE statistic_id = %s", (statistic_id,)
    )
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute("""
        INSERT INTO statistics_meta
            (statistic_id, source, unit_of_measurement, has_mean, has_sum, name, mean_type)
        VALUES (%s, %s, %s, 0, 1, %s, 0)
    """, (statistic_id, SOURCE, unit, "Heating Energy kWh"))
    return cursor.lastrowid


def get_previous_sum(cursor, meta_id, ts):
    cursor.execute(
        "SELECT sum FROM statistics WHERE metadata_id = %s AND start_ts < %s "
        "ORDER BY start_ts DESC LIMIT 1",
        (meta_id, ts)
    )
    row = cursor.fetchone()
    return float(row[0]) if row else 0.0


def delete_month_entries(cursor, meta_id, year, month):
    days_in_month = calendar.monthrange(year, month)[1]
    first_ts = get_day_ts(year, month, 1)
    last_ts  = get_day_ts(year, month, days_in_month) + 86400  # full last day
    cursor.execute(
        "DELETE FROM statistics WHERE metadata_id = %s "
        "AND start_ts >= %s AND start_ts <= %s",
        (meta_id, first_ts, last_ts)
    )
    cursor.execute(
        "DELETE FROM statistics_short_term WHERE metadata_id = %s "
        "AND start_ts >= %s AND start_ts <= %s",
        (meta_id, first_ts, last_ts)
    )


def get_meta_ids(cursor, sensor_ids):
    if not sensor_ids:
        return {}
    placeholders = ",".join(["%s"] * len(sensor_ids))
    cursor.execute(
        f"SELECT id, statistic_id FROM statistics_meta "
        f"WHERE statistic_id IN ({placeholders})",
        sensor_ids
    )
    return {row[1]: row[0] for row in cursor.fetchall()}


def get_daily_means(cursor, meta_ids, year, month):
    """Returns {day: avg_mean} for all days with data in the given month."""
    if not meta_ids:
        return {}
    days_in_month  = calendar.monthrange(year, month)[1]
    month_start_ts = get_day_ts(year, month, 1)
    month_end_ts   = get_day_ts(year, month, days_in_month) + 86400
    meta_id_list   = list(meta_ids.values())
    placeholders   = ",".join(["%s"] * len(meta_id_list))

    cursor.execute(f"""
        SELECT start_ts, mean FROM statistics
        WHERE metadata_id IN ({placeholders})
          AND start_ts >= %s AND start_ts < %s
          AND mean IS NOT NULL
        ORDER BY start_ts
    """, meta_id_list + [month_start_ts, month_end_ts])

    day_values = {}
    for ts, mean_val in cursor.fetchall():
        dt_local = datetime.fromtimestamp(float(ts), tz=LOCAL_TZ)
        if dt_local.year == year and dt_local.month == month:
            day_values.setdefault(dt_local.day, []).append(float(mean_val))

    return {day: sum(vals) / len(vals) for day, vals in day_values.items()}


def get_daily_weights(cursor, year, month):
    """
    Calculates per-day weights based on Tado heating percentages.
    Days without heating (warm + Tado = 0) get weight 0.
    Returns None if no data available → equal distribution as fallback.
    """
    days_in_month = calendar.monthrange(year, month)[1]

    tado_meta = get_meta_ids(cursor, TADO_SENSORS)
    if not tado_meta:
        print("  No Tado metadata in DB → equal distribution")
        return None

    tado_daily = get_daily_means(cursor, tado_meta, year, month)
    if not tado_daily:
        print("  No Tado statistics for this month → equal distribution")
        return None

    outdoor_meta  = get_meta_ids(cursor, [OUTDOOR_TEMP_SENSOR])
    outdoor_daily = get_daily_means(cursor, outdoor_meta, year, month) if outdoor_meta else {}

    days_with_data = len(tado_daily)
    overall_avg    = sum(tado_daily.values()) / days_with_data
    no_heating_days = 0
    weights_raw = {}

    for day in range(1, days_in_month + 1):
        tado_avg = tado_daily.get(day, overall_avg)
        outdoor  = outdoor_daily.get(day, None)

        if outdoor is not None and outdoor >= HEATING_CUTOFF_TEMP and tado_avg < 1.0:
            weights_raw[day] = 0.0
            no_heating_days += 1
        else:
            weights_raw[day] = max(tado_avg, 0.0)

    total = sum(weights_raw.values())
    if total == 0:
        print("  All weights = 0 (no heating?) → equal distribution")
        return None

    weights = {day: val / total for day, val in weights_raw.items()}
    active_days = sum(1 for w in weights.values() if w > 0)

    print(f"  Tado data for {days_with_data}/{days_in_month} days")
    if no_heating_days > 0:
        print(f"  {no_heating_days} days detected as 'central heating off' "
              f"(outdoor >= {HEATING_CUTOFF_TEMP}°C + Tado=0)")
    print(f"  Active heating days: {active_days}/{days_in_month}")

    return weights


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 heizenergie_import_single.py <date> <kwh>")
        sys.exit(1)

    datum_str = sys.argv[1]
    kwh_str   = sys.argv[2].replace(",", ".")

    try:
        dt_input = datetime.strptime(datum_str[:10], "%Y-%m-%d")
        year, month = dt_input.year, dt_input.month
    except ValueError:
        print(f"Invalid date: {datum_str}")
        sys.exit(1)

    try:
        kwh_total = float(kwh_str)
    except ValueError:
        print(f"Invalid kWh value: {kwh_str}")
        sys.exit(1)

    days_in_month = calendar.monthrange(year, month)[1]
    print(f"Importing: {month:02d}/{year} → {kwh_total} kWh, {days_in_month} days")

    conn   = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS,
                             database=DB_NAME, charset="utf8mb4")
    cursor = conn.cursor()

    print("Reading Tado heating percentage data...")
    weights = get_daily_weights(cursor, year, month)

    if weights is None:
        weight_per_day = 1.0 / days_in_month
        weights = {day: weight_per_day for day in range(1, days_in_month + 1)}
        print(f"Equal distribution: {round(kwh_total / days_in_month, 4)} kWh/day")
    else:
        print("Weighted distribution active (Tado data)")

    meta_id      = get_or_create_metadata(cursor, STATISTIC_ID, UNIT)
    delete_month_entries(cursor, meta_id, year, month)
    first_day_ts = get_day_ts(year, month, 1)
    cumulative   = get_previous_sum(cursor, meta_id, first_day_ts)
    now_ts       = datetime.now(timezone.utc).timestamp()

    print("\nDaily distribution:")
    for day in range(1, days_in_month + 1):
        kwh_day    = round(kwh_total * weights[day], 4)
        ts         = get_day_ts(year, month, day)
        cumulative = round(cumulative + kwh_day, 4)
        dt_label   = datetime(year, month, day).strftime("%d.%m")
        pct_label  = f"{weights[day]*100:.1f}%"
        marker     = " ← no heating" if weights[day] == 0 else ""
        print(f"  {dt_label}: {kwh_day:.3f} kWh  (weight {pct_label}){marker}")

        cursor.execute("""
            INSERT INTO statistics
                (created_ts, metadata_id, start_ts, state, sum, mean, min, max)
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL)
        """, (now_ts, meta_id, ts, kwh_day, cumulative))
        cursor.execute("""
            INSERT INTO statistics_short_term
                (created_ts, metadata_id, start_ts, state, sum, mean, min, max)
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL)
        """, (now_ts, meta_id, ts, kwh_day, cumulative))

    conn.commit()
    conn.close()
    print(f"\nDone! {days_in_month} daily entries saved.")
    print(f"Cumulative total: {cumulative:.2f} kWh")


if __name__ == "__main__":
    main()
