# Tado Heating Energy Tracker for Home Assistant

Track and estimate your heating energy consumption in Home Assistant using Tado thermostat valve positions as a proxy — even if you only receive actual consumption data once a month from your landlord or utility provider.

## What it does

**Monthly import (`heizenergie_import_single.py`)**
When you receive your monthly kWh reading, this script distributes the total across all days of the month — weighted by how much each Tado zone was actually heating that day. Days with warm outdoor temperatures and zero Tado activity are automatically treated as "central heating off" (summer detection).

**Daily estimation (`heizenergie_schaetzung.py`)**
Runs automatically every day and estimates the current month's consumption in real time. It learns a calibration factor from your historical actual readings and applies it to the current Tado activity. The more months of real data you have, the more accurate it gets.

## Requirements

- Home Assistant (any recent version)
- [MariaDB add-on](https://github.com/home-assistant/addons/tree/master/mariadb) (core-mariadb)
- [Tado integration](https://www.home-assistant.io/integrations/tado/) with heating percentage sensors
- An outdoor temperature sensor (used for summer detection)
- `pymysql` Python package

## Installation

### 1. Install pymysql

In your Home Assistant terminal (SSH add-on or terminal add-on):

```bash
pip3 install pymysql --target /config/deps
```

### 2. Copy the scripts

Copy both Python scripts to your `/config/` directory:
- `heizenergie_import_single.py`
- `heizenergie_schaetzung.py`

### 3. Configure the scripts

Open both scripts and adjust the configuration block at the top:

```python
# Database
DB_PASS = "YOUR_DB_PASSWORD"    # your MariaDB password
LOCAL_TZ = ZoneInfo("Europe/Berlin")  # your timezone

# Tado sensor entity IDs (one per zone)
TADO_SENSORS = [
    "sensor.living_room_heating",
    "sensor.kitchen_heating",
    # ... add all your zones
]

# Outdoor temperature sensor
OUTDOOR_TEMP_SENSOR = "sensor.outdoor_temperature"

# Statistic IDs (must match your template sensor entity IDs below)
STATISTIC_ID = "sensor.heating_energy_kwh"           # import script
STATISTIC_ID_ACTUAL   = "sensor.heating_energy_kwh"  # estimation script
STATISTIC_ID_ESTIMATE = "sensor.heating_energy_estimate_kwh"
```

> **Note:** Do not use umlauts (ä, ö, ü) in entity IDs. Home Assistant converts them silently, which causes mismatches. Use `ae`, `oe`, `ue` instead.

### 4. Add template sensors to configuration.yaml

```yaml
template:
  - sensor:
      - name: "Heating Energy kWh"
        unique_id: heating_energy_kwh
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
        state: "0"

      - name: "Heating Energy Estimate kWh"
        unique_id: heating_energy_estimate_kwh
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
        state: "0"
```

### 5. Add shell commands to configuration.yaml

```yaml
shell_command:
  heating_import: >
    python3 /config/heizenergie_import_single.py "{{ datum }}" "{{ kwh }}"
  heating_estimate: python3 /config/heizenergie_schaetzung.py
```

### 6. Create the daily automation

In Home Assistant go to **Settings → Automations → Add Automation** and use this YAML:

```yaml
alias: Heating Energy Estimate daily
trigger:
  - platform: time
    at: "03:00:00"
action:
  - action: shell_command.heating_estimate
mode: single
```

### 7. Add input helpers (for the import form)

In **Settings → Helpers** create:
- `input_datetime.heating_import_date` — date only
- `input_number.heating_import_kwh` — min: 0, max: 100000, step: 1, unit: kWh

### 8. Create a script for the import

```yaml
script:
  heating_import:
    alias: Import Heating Consumption
    sequence:
      - action: shell_command.heating_import
        data:
          datum: "{{ states('input_datetime.heating_import_date') }}"
          kwh: "{{ states('input_number.heating_import_kwh') }}"
```

### 9. Create the dashboard

Create a new dashboard (or add a view to an existing one) with the following Lovelace YAML:

```yaml
views:
  - title: Heating Energy
    cards:
      - type: entities
        title: 🔥 Enter monthly consumption
        entities:
          - entity: input_datetime.heating_import_date
            name: Select month
          - entity: input_number.heating_import_kwh
            name: Consumption (kWh)
          - type: button
            name: Import
            tap_action:
              action: call-service
              service: script.heating_import

      - type: statistics-graph
        title: 📊 Daily estimates (current month)
        entities:
          - entity: sensor.heating_energy_kwh
            name: Actual
          - entity: sensor.heating_energy_estimate_kwh
            name: Estimate
        stat_types:
          - change
        period: day
        days_to_show: 30
        chart_type: bar

      - type: statistic
        entity: sensor.heating_energy_kwh
        name: Actual consumption this month
        stat_type: change
        period:
          calendar:
            period: month
        icon: mdi:fire

      - type: statistic
        entity: sensor.heating_energy_estimate_kwh
        name: Estimated consumption this month
        stat_type: change
        period:
          calendar:
            period: month
        icon: mdi:chart-line
```

To create a new dashboard: **Settings → Dashboards → Add Dashboard**, then edit it and switch to YAML mode.

### 10. Restart Home Assistant

After restarting, verify both template sensors appear in **Developer Tools → States**.

## Usage

### Entering monthly actual consumption

When you receive your monthly kWh reading from your landlord or utility:

1. Open your dashboard (or Developer Tools → Actions)
2. Set the date to any day within that month
3. Enter the kWh value
4. Run the import script / press the import button

The script will distribute the total across the days of that month, weighted by Tado valve activity.

### Viewing estimates

The daily estimation runs automatically at 03:00.

**First run:** After installation, trigger it once manually so you don't have to wait until 03:00:

1. Go to **Developer Tools → Actions**
2. Search for `shell_command.heating_estimate`
3. Click **Perform action**

After that, the automation handles everything automatically every night.

> **Note:** The estimation sensor only appears in the statistics database after the first successful run. If the dashboard shows no data, trigger the script manually as described above.

Add `sensor.heating_energy_estimate_kwh` to your Energy Dashboard or a statistics-graph card to see daily estimates.

## How the calibration works

The estimation script calculates a calibration factor: **kWh per Tado-unit**.

For each month with actual data, it computes:
```
factor = actual_kWh / sum_of_daily_tado_averages
```

Newer months are weighted exponentially higher (weight = e^(0.3 * age_offset)), so the estimate adapts as your heating behavior or efficiency changes over time.

With just 1 month of actual data it already works. With 3+ months the factor stabilizes noticeably.

## Summer detection

Days where **all** of these conditions are true are treated as "central heating off":
- Daily average outdoor temperature ≥ `HEATING_CUTOFF_TEMP` (default: 15°C)
- All Tado zones at 0% heating

These days contribute 0 kWh to both the weighted distribution and the estimate.

You can adjust `HEATING_CUTOFF_TEMP` in the configuration block of both scripts.

## Troubleshooting

**Script returns "No Tado metadata in DB"**
The Tado sensors have not recorded any statistics yet. Make sure your Tado integration is running and wait 24 hours for statistics to accumulate.

**Script returns "Not enough actual data"**
Enter at least one month of actual consumption via the import script first.

**Estimation seems too high or too low**
The calibration factor needs more actual months to stabilize. After 3–4 months the estimates typically become quite accurate.

**Entity shows up as `sensor.heating_energy_kwh_2`**
You have a duplicate entity. Remove all template sensors with that name from `configuration.yaml`, then restart. Only keep one block with a `unique_id`.

## License

MIT
