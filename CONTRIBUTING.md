# Contributing

Thanks for considering a contribution. This project is a single-file Python CLI;
keep changes focused and readable.

## Project layout

- `update_knx_config.py` — interactive CLI for single-entity create/delete.
  Contains the WebSocket layer and per-platform config builders.
- `parse_knx_project.py` — batch parser for ETS `.knxproj` files.
  Uses `xknxproject` to extract entities and delegates creation to
  `update_knx_config`.
- `requirements.txt` — Python dependencies.
- `README.md` — user-facing documentation.
- `LICENSE` — MIT.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Test against a running Home Assistant instance with the KNX integration loaded:

```bash
export HA_TOKEN="..."   # long-lived access token; --token also accepted
python3 update_knx_config.py \
  --url "ws://localhost:8123/api/websocket" \
  --entity-type light \
  --name "Test Light" \
  --address "1/1/1"
```

Verify the entity in the HA UI, then clean up:

```bash
python3 update_knx_config.py --delete light.test_light
```

## Code structure

### WebSocket layer

- `connect_and_authenticate()` — HA WebSocket auth handshake.
- `send_ws_message()` — generic send/receive with caller-supplied unique IDs.
- Thin wrappers per KNX command (`get_entity_schema`, `validate_entity`,
  `create_entity`, `delete_entity`, …).

### Config builders

One per platform, returning the `{"entity": …, "knx": …}` payload:

- `get_light_config()` — switch, brightness, color_temp, color (RGB/RGBW/xyY).
- `get_switch_config()` — on/off with optional `invert`.
- `get_binary_sensor_config()` — state-only sensor with `device_class`.
- `get_climate_config()` — temperature, setpoint, operation mode, on/off.
- `get_cover_config()` — up/down, stop, position, travel times.

### CLI entry point

`argparse`-based dispatcher in `main()`. Workflow:

1. Connect and authenticate.
2. Fetch base data + entity schema (mostly informational; payload is built
   client-side).
3. Build the platform-specific payload via the appropriate `get_*_config()`.
4. Call `knx/validate_entity`. If validation fails, abort.
5. Call `knx/create_entity`.

## KNX WebSocket commands used

| Command                  | Notes                                       |
|--------------------------|---------------------------------------------|
| `knx/get_base_data`      | Returns supported platforms.                |
| `knx/get_entity_entries` | Lists configured entities.                  |
| `knx/get_schema`         | Requires `platform`.                        |
| `knx/validate_entity`    | Requires `platform` + `data`.               |
| `knx/create_entity`      | Requires `platform` + `data`.               |
| `knx/update_entity`      | Requires `platform` + `data` + `entity_id`. |
| `knx/delete_entity`      | Requires `entity_id`.                       |

## Schema rules to keep in mind

1. **Payload shape.** All entities use `{"entity": {...}, "knx": {...}}`. The
   `knx` block holds the platform-specific address config.

2. **`color` is nested.** RGB/RGBW/xyY color uses a `knx_group_select`
   wrapper, not a flat `ga_color` at the `knx` root:

   ```json
   "color": {
     "ga_color": {"write": "...", "state": "...", "dpt": "..."}
   }
   ```

3. **Common DPTs.**
   - `"251.600"` — RGBW (4 bytes)
   - `"232.600"` — RGB (3 bytes)
   - `"242.600"` — xyY color
   - `"7.600"` — color temperature (2 bytes)

4. **`ga_color_temp` requires a `dpt`.**

5. **Binary sensor uses `state`, not `write`.** The `ga_sensor` group address
   selector for `binary_sensor` rejects `write` — it only accepts `state` (and
   optionally `passive`). The `binary_sensor` schema is read-only by design.

6. **Unique message IDs.** Every WebSocket message needs a fresh `id`;
   reusing IDs returns an `id_reuse` error from HA. `main()` increments
   `msg_id` after each send.

7. **Validate before create.** Always call `knx/validate_entity` first.
   Note that the WebSocket envelope's top-level `success` only indicates
   the call round-tripped — the validation verdict is in `result.success`.
   Check the inner field, not the envelope.

## KNX address fields by platform

### Light
- `ga_switch` — on/off (write + optional state)
- `ga_brightness` — dimming (write + optional state)
- `ga_color_temp` — tunable white (with `dpt`)
- `color` → `ga_color` — RGB/RGBW/xyY color (with `dpt`)
- Not exposed via the CLI: `individual_colors` (per-channel actuators)

### Switch
- `ga_switch` — on/off (write + optional state)
- `invert` — boolean

### Binary sensor
- `knx.ga_sensor` — read-only (`state` is the address listened to on the bus)
- `entity.device_class` — optional, lives under `entity`, not `knx`
  (`motion`, `door`, `window`, …)

### Climate
- `ga_temperature_current` — current temperature, state-only (DPT 9.001)
- `target_temperature` → `ga_temperature_target` — setpoint (DPT 9.001)
- `ga_operation_mode` — optional mode control (DPT 20.102)
- `ga_on_off` — optional on/off switch (DPT 1)
- `default_controller_mode` — exposed via `--controller-mode`; one of
  `off`, `heat`, `cool`, `heat_cool`, `auto`, `dry`, `fan_only`

### Cover
- `ga_up_down` — movement (write)
- `ga_stop` — stop command (optional write)
- `ga_position_set` — position setpoint (optional write)
- `ga_position_state` — position feedback (optional state)
- `travelling_time_up` / `travelling_time_down` — seconds (default: 60)

## Adding a new platform

1. Add the platform to the `--entity-type` `choices` list.
2. Write a `get_<platform>_config()` builder.
3. Add a dispatch branch in `main()`.
4. Verify field names against `knx/get_schema` on a live HA instance — the
   schema is the source of truth and has changed across KNX integration
   releases.

## KNX project parser architecture

`parse_knx_project.py` follows a strict data-driven design:

### Classification chain

1. **`_classify_com_object(co)`** — inspects a single communication object:
   DPT numbers, flag patterns (`[W--]`, `[-TR]`, `[WTR]`, `[W-R]`).
2. **`_classify_device(dev, com_objects)`** — aggregates all communication
   objects on a device to determine its role:
   - `is_pure_actuator` — has DPT 1 `[W--]` (accepts bus commands)
   - `is_lighting_gateway` — has DPT 5 (brightness) or DPT 3 (dimming)
   - `is_sensor_input` — originates DPT 1 sensor readings, no write-only
   - `is_thermostat_controller` — has DPT 9 AND write-capable DPT 1
3. **`_classify_ga(ga, com_objects, devices)`** — aggregates device roles
   across all devices linked to a group address.

### Entity builders

Each builder (`_build_light`, `_build_switch`, `_build_climate`,
`_build_cover`) receives a group of related GAs (from a KNX function) and
uses only DPT + flags + device roles to determine which GA is the switch
write, switch state, brightness, setpoint, etc.

No manufacturer-specific com-object names are used. No
language-dependent hardware names are matched. The classifiers work
across different manufacturers (ABB, MDT, Gira, Siemens, Zennio) and ETS
language versions without modification.

### Entity extraction

1. **Functions first.** The KNX project's own function definitions
   (FT-1/6/8/10) are the primary source — they tell us which GAs belong
   together and what entity type they represent.
2. **Unmapped fallback.** GAs not in any function are analyzed via
   `_classify_ga`: relay pairs are grouped as switches, binary inputs
   become binary sensors.

## Reporting issues

Please include:

- HA Core version and KNX integration version (visible in
  `knx/get_base_data` → `connection_info.version`).
- The full JSON payload the script sent, and the response from HA.
- A minimal `--entity-type … --address …` invocation that reproduces.
