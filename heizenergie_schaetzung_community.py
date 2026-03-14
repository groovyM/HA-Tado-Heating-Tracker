#!/usr/bin/env python3
"""
Heating Energy Daily Estimation for Home Assistant
====================================================
Runs daily (via HA automation) and writes a running estimate of the current
month's heating consumption based on Tado thermostat valve positions.

How it works:
  1. Reads all previously entered actual consumption values from the DB
  2. Reads the monthly Tado heating percentage averages for those same months
  3. Calculates a kWh-per-%-unit calibration factor (newer months weighted higher)
  4. Applies the factor to Tado data from the current month up to today
  5. Filters out "no heating" days (outdoor temp above threshold AND Tado = 0%)
  6. Writes daily estimated values to statistics/statistics_short_term

The calibration factor improves automatically with each new actual month entered.

Run via shell_command:
  python3 /config/heizenergie_schaetzung.py

Requirements:
  - Home Assistant with MariaDB (core-mariadb add-on)
  - Tado integration with heating percentage sensors
  - Outdoor temperature sensor
  - At least 1 month of actual consumption data entered
  - pymysql: pip3 install pymysql --target /config/deps
"""

import sys
sys.path.insert(0, "/config/deps")
import pymysql
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import calendar
import math

# ── Configuration ─────────────────────────────────────────────────────────────
# Database connection (MariaDB add-on defaults)
DB_HOST = "core-mariadb"
DB_USER = "homeassistant"
DB_PASS = "YOUR_DB_PASSWORD"   # ← change this
DB_NAME = "homeassistant"

# Statistic IDs — must match entity_ids of your template sensors
STATISTIC_ID_ACTUAL   = "sensor.heating_energy_kwh"
STATISTIC_ID_ESTIMATE = "sensor.heating_energy_estimate_kwh"

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

# Outdoor temperature sensor
OUTDOOR_TEMP_SENSOR = "sensor.outdoor_temperature"

# Days with average outdoor temp >= this AND Tado = 0% → treated as "no central heating"
HEATING_CUTOFF_TEMP = 15.0   # °C

# Minimum number of actual months needed before estimation starts
MIN_ACTUAL_MONTHS = 1
# ──────────────────────────────────────────────────────────────────────────────


def get_day_ts(year, month, day):
    dt_local = datetime(year, month, day, 0, 0, 0, tzinfo=LOCAL_TZ)
    return dt_local.astimezone(timezone.utc).timestamp()


def get_or_create_metadata(cursor, statistic_id, unit, name):
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
    """, (statistic_id, SOURCE, unit, name))
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


def get_daily_means_for_month(cursor, meta_ids, year, month):
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


def get_monthly_tado_sum(cursor, tado_meta, outdoor_meta, year, month):
    """
    Returns the monthly 'effective heating activity' as sum of daily Tado
    averages — only counting days with active central heating.
    """
    days_in_month  = calendar.monthrange(year, month)[1]
    tado_daily     = get_daily_means_for_month(cursor, tado_meta, year, month)
    outdoor_daily  = get_daily_means_for_month(cursor, outdoor_meta, year, month)

    if not tado_daily:
        return 0.0

    overall_avg = sum(tado_daily.values()) / len(tado_daily)
    total = 0.0

    for day in range(1, days_in_month + 1):
        tado_avg = tado_daily.get(day, overall_avg)
        outdoor  = outdoor_daily.get(day, None)

        if outdoor is not None and outdoor >= HEATING_CUTOFF_TEMP and tado_avg < 1.0:
            continue  # summer / no heating → skip

        total += max(tado_avg, 0.0)

    return total


def get_actual_months(cursor, actual_meta_id):
    """
    Reads all entered actual consumption months from the statistics table.
    Returns list of (year, month, kwh).
    """
    cursor.execute("""
        SELECT start_ts, sum FROM statistics
        WHERE metadata_id = %s
        ORDER BY start_ts
    """, (actual_meta_id,))
    rows = cursor.fetchall()
    if not rows:
        return []

    months = {}
    for ts, sum_val in rows:
        dt  = datetime.fromtimestamp(float(ts), tz=LOCAL_TZ)
        key = (dt.year, dt.month)
        if key not in months:
            months[key] = {"first_sum": float(sum_val), "last_sum": float(sum_val)}
        else:
            months[key]["last_sum"] = float(sum_val)

    result = []
    for key in sorted(months.keys()):
        year, month = key
        month_kwh = months[key]["last_sum"] - months[key]["first_sum"]
        if month_kwh > 0:
            result.append((year, month, round(month_kwh, 2)))

    return result


def calculate_factor(actual_months, cursor, tado_meta, outdoor_meta):
    """
    Calculates kWh-per-Tado-unit calibration factor.
    Newer months are weighted exponentially higher.
    Returns None if not enough data.
    """
    if len(actual_months) < MIN_ACTUAL_MONTHS:
        return None

    factors  = []
    weights  = []
    n        = len(actual_months)

    for i, (year, month, kwh) in enumerate(actual_months):
        tado_sum = get_monthly_tado_sum(cursor, tado_meta, outdoor_meta, year, month)
        if tado_sum <= 0:
            print(f"  {month:02d}/{year}: Tado sum = 0, skipped")
            continue

        factor = kwh / tado_sum
        weight = math.exp(0.3 * (i - (n - 1)))   # newer = higher weight
        factors.append(factor)
        weights.append(weight)
        print(f"  {month:02d}/{year}: {kwh:.1f} kWh / {tado_sum:.1f} Tado units "
              f"= {factor:.4f} kWh/unit (weight {weight:.2f})")

    if not factors:
        return None

    return sum(f * w for f, w in zip(factors, weights)) / sum(weights)


def main():
    now_local     = datetime.now(tz=LOCAL_TZ)
    year          = now_local.year
    month         = now_local.month
    today         = now_local.day
    days_in_month = calendar.monthrange(year, month)[1]

    print(f"Heating Energy Estimate for {month:02d}/{year} "
          f"(as of {today:02d}.{month:02d}.{year})")

    conn   = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS,
                             database=DB_NAME, charset="utf8mb4")
    cursor = conn.cursor()

    tado_meta    = get_meta_ids(cursor, TADO_SENSORS)
    outdoor_meta = get_meta_ids(cursor, [OUTDOOR_TEMP_SENSOR])
    actual_ids   = get_meta_ids(cursor, [STATISTIC_ID_ACTUAL])

    if not tado_meta:
        print("No Tado metadata in DB → estimation not possible")
        conn.close()
        sys.exit(0)

    if STATISTIC_ID_ACTUAL in actual_ids:
        all_months    = get_actual_months(cursor, actual_ids[STATISTIC_ID_ACTUAL])
        actual_months = [(y, m, kwh) for y, m, kwh in all_months
                         if (y, m) < (year, month)]
    else:
        actual_months = []

    print(f"\nActual data: {len(actual_months)} months available")
    print("\nCalibration factor calculation:")
    factor = calculate_factor(actual_months, cursor, tado_meta, outdoor_meta)

    if factor is None:
        print(f"Not enough actual data yet (need at least {MIN_ACTUAL_MONTHS} month)")
        print("Please enter your first actual monthly consumption value.")
        conn.close()
        sys.exit(0)

    print(f"\n→ Weighted calibration factor: {factor:.4f} kWh per Tado unit")

    tado_daily    = get_daily_means_for_month(cursor, tado_meta, year, month)
    outdoor_daily = get_daily_means_for_month(cursor, outdoor_meta, year, month)

    if not tado_daily:
        print("No Tado data for current month → estimation not possible")
        conn.close()
        sys.exit(0)

    overall_avg = sum(tado_daily.values()) / len(tado_daily)

    est_meta_id = get_or_create_metadata(
        cursor, STATISTIC_ID_ESTIMATE, UNIT, "Heating Energy Estimate kWh"
    )
    delete_month_entries(cursor, est_meta_id, year, month)

    # Estimate resets to 0 each month (no cumulative carry-over between months)
    first_day_ts = get_day_ts(year, month, 1)
    cumulative   = 0.0
    now_ts       = datetime.now(timezone.utc).timestamp()

    print("\nDaily estimates:")
    days_heating    = 0
    days_no_heating = 0

    for day in range(1, days_in_month + 1):
        ts = get_day_ts(year, month, day)

        if day > today:
            continue  # future days: no entry yet

        tado_avg = tado_daily.get(day, overall_avg)
        outdoor  = outdoor_daily.get(day, None)

        if outdoor is not None and outdoor >= HEATING_CUTOFF_TEMP and tado_avg < 1.0:
            kwh_day = 0.0
            days_no_heating += 1
            marker = " ← no central heating"
        else:
            kwh_day = round(tado_avg * factor, 4)
            days_heating += 1
            marker = ""

        cumulative   = round(cumulative + kwh_day, 4)
        dt_label     = datetime(year, month, day).strftime("%d.%m")
        outdoor_str  = f"{outdoor:.1f}°C" if outdoor is not None else "n/a"
        print(f"  {dt_label}: {kwh_day:.3f} kWh  "
              f"(Tado: {tado_avg:.1f}%, outdoor: {outdoor_str}){marker}")

        cursor.execute("""
            INSERT INTO statistics
                (created_ts, metadata_id, start_ts, state, sum, mean, min, max)
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL)
        """, (now_ts, est_meta_id, ts, kwh_day, cumulative))
        cursor.execute("""
            INSERT INTO statistics_short_term
                (created_ts, metadata_id, start_ts, state, sum, mean, min, max)
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL)
        """, (now_ts, est_meta_id, ts, kwh_day, cumulative))

    conn.commit()
    conn.close()

    print(f"\nDone!")
    print(f"  Heating days so far:    {days_heating}")
    print(f"  Days without heating:   {days_no_heating}")
    print(f"  Estimated consumption:  {cumulative:.2f} kWh")


if __name__ == "__main__":
    main()
