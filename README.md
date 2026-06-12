# KNX Entity Creator

CLI tool for creating and managing KNX entities in Home Assistant via the KNX integration's
WebSocket API.

## Prerequisites

- Home Assistant with the [KNX integration](https://www.home-assistant.io/integrations/knx/) installed
- A [Long-Lived Access Token](https://www.home-assistant.io/docs/authentication/#your-account-profile)
- Python 3.8+ and `pip install -r requirements.txt`

The token can be passed via `--token` or via the `HA_TOKEN` environment variable
(recommended — `--token` is visible in `ps aux`).

## Usage

### Create a simple light

```bash
export HA_TOKEN="..."   # long-lived access token
python3 update_knx_config.py \
  --url "ws://homeassistant.local:8123/api/websocket" \
  --entity-type light \
  --name "Living Room Light" \
  --address "1/1/1"
```

### Create a dimmable light with state feedback

```bash
python3 update_knx_config.py \
  --url "ws://homeassistant.local:8123/api/websocket" \
  --entity-type light \
  --name "Kitchen Light" \
  --address "2/0/84" \
  --state-address "2/0/85" \
  --brightness-address "2/0/87" \
  --brightness-state-address "2/0/88"
```

### Create an RGBW light

```bash
python3 update_knx_config.py \
  --entity-type light \
  --name "Pool Light" \
  --address "2/0/84" \
  --state-address "2/0/85" \
  --brightness-address "2/0/87" \
  --brightness-state-address "2/0/88" \
  --color-address "2/0/101" \
  --color-state-address "2/0/102" \
  --color-dpt "251.600"
```

### Create a switch

```bash
python3 update_knx_config.py \
  --entity-type switch \
  --name "Garage Switch" \
  --address "1/0/1" \
  --state-address "1/0/2"
```

### Create a binary sensor

```bash
python3 update_knx_config.py \
  --entity-type binary_sensor \
  --name "Motion Hallway" \
  --address "6/0/2" \
  --device-class motion
```

### Create a climate (thermostat)

```bash
python3 update_knx_config.py \
  --entity-type climate \
  --name "Living Room Thermostat" \
  --temperature-state "1/6/60" \
  --setpoint-write "1/6/63" \
  --setpoint-state "1/6/64" \
  --controller-mode heat
```

### Delete an entity

```bash
python3 update_knx_config.py --delete "light.living_room_light"
```

## Project Parser — `parse_knx_project.py`

Parses ETS `.knxproj` project files and bulk-creates all KNX entities found.

Uses the [`xknxproject`](https://github.com/XKNX/xknxproject) library to extract
group addresses, communication objects, and KNX functions from a project file,
then determines the correct Home Assistant entity type using **only** KNX-level
data: DPT numbers, communication-object flags, and device roles. No
manufacturer-specific names, no language-dependent text matching.

### Prerequisites

- `pip install -r requirements.txt` (adds `xknxproject>=3.7.0`)
- An ETS `.knxproj` project file (ETS5 or ETS6)
- Project password if the file is encrypted

### Usage

```bash
# Dry-run — see what would be created (default, safe)
python3 parse_knx_project.py \
  --project project.knxproj \
  --password "ets-password"

# Create all entities in Home Assistant
python3 parse_knx_project.py \
  --project project.knxproj \
  --password "ets-password" \
  --url "ws://homeassistant.local:8123/api/websocket" \
  --token "$HA_TOKEN" \
  --create \
  --skip-existing

# Export entity configs to JSON
python3 parse_knx_project.py \
  --project project.knxproj \
  --password "ets-password" \
  --output-json entities.json

# Filter by entity type or location
python3 parse_knx_project.py \
  --project project.knxproj \
  --password "ets-password" \
  --create \
  --filter-type light \
  --filter-location "Living Room"
```

### How it works

1. **Parse** the `.knxproj` via `xknxproject` → group addresses, devices,
   communication objects, KNX functions.
2. **Classify** each group address using DPT numbers, com-object flags
   (`write`/`transmit`/`read`), and device roles (actuator vs. sensor vs.
   lighting-gateway vs. thermostat-controller). No name matching.
3. **Group** related addresses into Home Assistant entities using the KNX
   project's function definitions (FT-1 = switchable light, FT-6 = dimmable
   light, FT-8 = heating, FT-10 = switchable socket).
4. **Create** entities via `update_knx_config`'s WebSocket API — validate,
   then create, skipping existing entities when requested.

### Entity type mapping

| KNX Function | ETS usage | HA Platform |
|-------------|-----------|-------------|
| FT-1 | switchable light | `light` |
| FT-6 | dimmable light | `light` (with brightness) |
| FT-8 | heating | `climate` |
| FT-10 | switchable socket | `switch` |
| FT-0 (custom) | varies | heuristic (DPT-based) |
| Unmapped binary inputs | — | `binary_sensor` (detected by device role) |

### CLI Reference

| Argument | Description |
|---|---|
| `--project` | Path to `.knxproj` file (required) |
| `--password` | ETS project password. Also settable via `KNX_PROJECT_PASSWORD` env var |
| `--create` | Actually create entities. Without this flag, performs a dry-run |
| `--url` | HA WebSocket URL (default: `ws://localhost:8123/api/websocket`) |
| `--token` | Long-lived access token. Falls back to `HA_TOKEN` env var |
| `--skip-existing` | Skip entities already configured in Home Assistant |
| `--output-json` | Write extracted entity configs to a JSON file |
| `--no-meta` | Omit `_meta` fields from JSON output |
| `--filter-type` | Only process `light`, `switch`, `binary_sensor`, `climate`, or `cover` |
| `--filter-location` | Only process entities in a given room/location |
| `--verbose` / `-v` | Verbose output with entity details |

## CLI Reference

| Argument | Description |
|---|---|
| `--url` | WebSocket URL (default: `ws://localhost:8123/api/websocket`) |
| `--token` | Long-lived access token (or set `HA_TOKEN` env var) |
| `--entity-type` | `light`, `switch`, `binary_sensor`, `climate`, or `cover` |
| `--name` | Entity display name |
| `--address` | Primary KNX group address |
| `--state-address` | State feedback address |
| `--brightness-address` | Brightness control address (light) |
| `--brightness-state-address` | Brightness state address (light) |
| `--color-address` | Combined RGB/RGBW color address (light) |
| `--color-state-address` | Color state address (light) |
| `--color-dpt` | DPT for color — `"251.600"` (RGBW), `"232.600"` (RGB), `"242.600"` (xyY) |
| `--invert` | Invert switch state (switch) |
| `--device-class` | Device class (binary_sensor) |
| `--temperature-state` | Current temperature state address (climate) |
| `--setpoint-write` | Setpoint write address (climate) |
| `--setpoint-state` | Setpoint state address (climate) |
| `--operation-mode-write` | Operation mode write address (climate) |
| `--operation-mode-state` | Operation mode state address (climate) |
| `--on-off-write` | On/off write address (climate) |
| `--on-off-state` | On/off state address (climate) |
| `--controller-mode` | HVAC controller mode for climate: `off`, `heat` (default), `cool`, `heat_cool`, `auto`, `dry`, `fan_only` |
| `--stop-write` | Stop write address (cover) |
| `--position-set-write` | Position set write address (cover) |
| `--position-state` | Position state address (cover) |
| `--travelling-time-up` / `--travelling-time-down` | Cover travel time in seconds (default: 60) |
| `--delete` | Delete entity by entity_id |

## Entity Payload Schemas

All entities follow a two-key structure: `entity` (display config) and `knx` (KNX address config).

### Light

```json
{
  "entity": {"name": "Light"},
  "knx": {
    "ga_switch": {"write": "1/1/1"}
  }
}
```

Dimmable with feedback:

```json
{
  "entity": {"name": "Dimmable Light"},
  "knx": {
    "ga_switch": {"write": "2/0/84", "state": "2/0/85"},
    "ga_brightness": {"write": "2/0/87", "state": "2/0/88"}
  }
}
```

RGBW:

```json
{
  "entity": {"name": "RGBW Light"},
  "knx": {
    "ga_switch": {"write": "2/0/84", "state": "2/0/85"},
    "ga_brightness": {"write": "2/0/87", "state": "2/0/88"},
    "color": {
      "ga_color": {"write": "2/0/101", "state": "2/0/102", "dpt": "251.600"}
    }
  }
}
```

Tunable white:

```json
{
  "entity": {"name": "TW Light"},
  "knx": {
    "ga_switch": {"write": "2/1/44", "state": "2/1/45"},
    "ga_brightness": {"write": "2/1/47", "state": "2/1/48"},
    "ga_color_temp": {"write": "2/1/49", "state": "2/1/50", "dpt": "7.600"}
  }
}
```

### Switch

```json
{
  "entity": {"name": "Switch"},
  "knx": {
    "ga_switch": {"write": "1/0/1"}
  }
}
```

### Binary Sensor

```json
{
  "entity": {"name": "Motion Sensor", "device_class": "motion"},
  "knx": {
    "ga_sensor": {"state": "6/0/2"}
  }
}
```

### Climate

```json
{
  "entity": {"name": "Thermostat"},
  "knx": {
    "ga_temperature_current": {"state": "1/6/60"},
    "target_temperature": {
      "ga_temperature_target": {"write": "1/6/63", "state": "1/6/64"}
    },
    "ga_operation_mode": {"write": "1/6/65", "state": "1/6/66"},
    "default_controller_mode": "heat"
  }
}
```

### Cover

```json
{
  "entity": {"name": "Blind"},
  "knx": {
    "ga_up_down": {"write": "3/0/1"},
    "ga_stop": {"write": "3/0/2"},
    "ga_position_set": {"write": "3/0/3"},
    "travelling_time_up": 60,
    "travelling_time_down": 60
  }
}
```

## KNX WebSocket API

The script uses the following WebSocket commands exposed by the KNX integration:

| Command | Description |
|---|---|
| `knx/get_base_data` | Get supported platforms |
| `knx/get_entity_entries` | List all configured KNX entities |
| `knx/get_schema` | Get validation schema for a platform |
| `knx/validate_entity` | Validate entity data before creation |
| `knx/create_entity` | Create a new entity |
| `knx/update_entity` | Update an existing entity |
| `knx/delete_entity` | Delete an entity |

## Architecture

KNX entity configurations are stored in Home Assistant at:
`{config_dir}/.storage/knx/config_store.json`

The script authenticates via WebSocket, fetches the schema to verify field names,
validates the payload, then creates the entity — all through the KNX integration's
dedicated WebSocket API (no config file manipulation needed).

## License

MIT — see [LICENSE](LICENSE).
