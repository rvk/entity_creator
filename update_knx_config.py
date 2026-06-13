"""KNX Entity Creator — CLI for creating/deleting KNX entities in Home Assistant
via the KNX integration's WebSocket API.
"""

import argparse
import asyncio
import json
import os
import sys

import websockets

KNX_WEBSOCKET_COMMANDS = {
    "get_base_data": "knx/get_base_data",
    "create_entity": "knx/create_entity",
    "update_entity": "knx/update_entity",
    "delete_entity": "knx/delete_entity",
    "get_entity_entries": "knx/get_entity_entries",
    "get_entities_by_group": "knx/get_entities_by_group",
    "validate_entity": "knx/validate_entity",
    "get_schema": "knx/get_schema",
    "floor_list": "config/floor_registry/list",
    "floor_create": "config/floor_registry/create",
    "area_list": "config/area_registry/list",
    "area_create": "config/area_registry/create",
    "entity_update": "config/entity_registry/update",
    "entity_remove": "config/entity_registry/remove",
}

CLIMATE_CONTROLLER_MODES = (
    "off",
    "heat",
    "cool",
    "heat_cool",
    "auto",
    "dry",
    "fan_only",
)


# ---------------------------------------------------------------------------
# WebSocket layer
# ---------------------------------------------------------------------------


async def send_ws_message(websocket, msg_id, msg_type, payload=None):
    """Send a WebSocket message and return the parsed JSON response."""
    msg = {"id": msg_id, "type": msg_type}
    if payload:
        msg.update(payload)
    await websocket.send(json.dumps(msg))
    response = await websocket.recv()
    return json.loads(response)


async def connect_and_authenticate(url, token):
    """Connect to HA WebSocket and authenticate. Closes the socket on failure."""
    websocket = await websockets.connect(url)
    try:
        auth_data = json.loads(await websocket.recv())
        if auth_data.get("type") != "auth_required":
            raise ConnectionError(f"Unexpected initial message: {auth_data}")

        await websocket.send(
            json.dumps(
                {
                    "type": "auth",
                    "access_token": token,
                }
            )
        )

        result_data = json.loads(await websocket.recv())
        if result_data.get("type") != "auth_ok":
            raise ConnectionError(f"Authentication failed: {result_data}")
    except BaseException:
        await websocket.close()
        raise

    print("Authenticated successfully.")
    return websocket


async def get_knx_base_data(websocket, msg_id):
    return await send_ws_message(
        websocket, msg_id, KNX_WEBSOCKET_COMMANDS["get_base_data"]
    )


async def get_entity_schema(websocket, msg_id, platform):
    return await send_ws_message(
        websocket,
        msg_id,
        KNX_WEBSOCKET_COMMANDS["get_schema"],
        {"platform": platform},
    )


async def get_entity_entries(websocket, msg_id):
    return await send_ws_message(
        websocket, msg_id, KNX_WEBSOCKET_COMMANDS["get_entity_entries"]
    )


async def get_entities_by_group(websocket, msg_id):
    """Return the map of group address -> entity identifiers already bound.

    The KNX integration answers with ``{result: {"1/2/3": [...], ...}}`` so the
    keys are every group address currently used by an existing entity. Matching
    on group address is the reliable dedup signal (entity unique_ids are
    assigned server-side at creation and cannot be predicted here).
    """
    return await send_ws_message(
        websocket, msg_id, KNX_WEBSOCKET_COMMANDS["get_entities_by_group"]
    )


async def validate_entity(websocket, msg_id, platform, data):
    return await send_ws_message(
        websocket,
        msg_id,
        KNX_WEBSOCKET_COMMANDS["validate_entity"],
        {"platform": platform, "data": data},
    )


async def create_entity(websocket, msg_id, platform, data):
    return await send_ws_message(
        websocket,
        msg_id,
        KNX_WEBSOCKET_COMMANDS["create_entity"],
        {"platform": platform, "data": data},
    )


async def delete_entity(websocket, msg_id, entity_id):
    return await send_ws_message(
        websocket,
        msg_id,
        KNX_WEBSOCKET_COMMANDS["delete_entity"],
        {"entity_id": entity_id},
    )


async def list_floors(websocket, msg_id):
    """Fetch all floors from Home Assistant.

    Returns the ``config/floor_registry/list`` response, whose ``result`` is a
    list of floor objects with ``floor_id``, ``name``, ``level``, etc.
    """
    return await send_ws_message(
        websocket, msg_id, KNX_WEBSOCKET_COMMANDS["floor_list"]
    )


async def create_floor(websocket, msg_id, name, level=None):
    """Create a floor in Home Assistant.

    Args:
        name: Floor display name (must be unique).
        level: Optional integer floor level (higher = higher floor;
            negative for basements).

    Returns the ``config/floor_registry/create`` response. On success the
    ``result`` contains the created floor object including its ``floor_id``.
    """
    payload = {"name": name}
    if level is not None:
        payload["level"] = level
    return await send_ws_message(
        websocket, msg_id, KNX_WEBSOCKET_COMMANDS["floor_create"], payload
    )


async def list_areas(websocket, msg_id):
    """Fetch all areas from Home Assistant.

    Returns the ``config/area_registry/list`` response, whose ``result`` is a
    list of area objects with ``area_id``, ``name``, ``floor_id``, etc.
    """
    return await send_ws_message(websocket, msg_id, KNX_WEBSOCKET_COMMANDS["area_list"])


async def create_area(websocket, msg_id, name, floor_id=None):
    """Create an area in Home Assistant, optionally assigned to a floor.

    Args:
        name: Area display name.
        floor_id: Optional floor ID to assign this area to.

    Returns the ``config/area_registry/create`` response. On success the
    ``result`` contains the created area object including its ``area_id``.
    """
    payload = {"name": name}
    if floor_id:
        payload["floor_id"] = floor_id
    return await send_ws_message(
        websocket, msg_id, KNX_WEBSOCKET_COMMANDS["area_create"], payload
    )


async def update_entity_area(websocket, msg_id, entity_id, area_id):
    """Assign an entity to an area via the entity registry.

    Uses ``config/entity_registry/update`` to set the ``area_id`` on an
    existing entity without changing other properties.

    Args:
        entity_id: The entity to update (e.g. ``"light.kitchen_light"``).
        area_id: The area ID to assign the entity to.
    """
    return await send_ws_message(
        websocket,
        msg_id,
        KNX_WEBSOCKET_COMMANDS["entity_update"],
        {"entity_id": entity_id, "area_id": area_id},
    )


async def remove_entity_registry_entry(websocket, msg_id, entity_id):
    """Remove an entity from the Home Assistant entity registry.

    This is the counterpart to ``knx/delete_entity`` which only removes
    the entity from the KNX config store.  An entity must be removed from
    BOTH stores to avoid orphaned registry entries that block future
    entity ID reuse.

    Args:
        entity_id: The entity to remove (e.g. ``"light.kitchen_light"``).
    """
    return await send_ws_message(
        websocket,
        msg_id,
        KNX_WEBSOCKET_COMMANDS["entity_remove"],
        {"entity_id": entity_id},
    )


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


def get_light_config(
    name,
    address,
    state_address=None,
    brightness_address=None,
    brightness_state_address=None,
    color_temp_address=None,
    color_address=None,
    color_state_address=None,
    color_dpt=None,
):
    """Build a `light` entity payload.

    Args:
        name: Entity name.
        address: Primary group address for on/off (e.g., "1/1/0").
        state_address: State address for switch state (optional).
        brightness_address: Address for brightness control (optional).
        brightness_state_address: State address for brightness (optional).
        color_temp_address: Address for color temperature (optional).
        color_address: Address for color control (RGB/RGBW/xyY, optional).
        color_state_address: State address for color (optional).
        color_dpt: DPT for color, e.g. "251.600" (RGBW), "232.600" (RGB),
            "242.600" (xyY).
    """
    knx_config = {}

    ga_switch = {"write": address}
    if state_address:
        ga_switch["state"] = state_address
    knx_config["ga_switch"] = ga_switch

    if brightness_address:
        ga_brightness = {"write": brightness_address}
        if brightness_state_address:
            ga_brightness["state"] = brightness_state_address
        knx_config["ga_brightness"] = ga_brightness

    if color_temp_address:
        knx_config["ga_color_temp"] = {"write": color_temp_address}

    if color_address:
        ga_color = {"write": color_address}
        if color_state_address:
            ga_color["state"] = color_state_address
        if color_dpt:
            ga_color["dpt"] = color_dpt
        knx_config["color"] = {"ga_color": ga_color}

    return {
        "entity": {"name": name},
        "knx": knx_config,
    }


def get_switch_config(name, address, state_address=None, invert=False):
    """Build a `switch` entity payload.

    - ga_switch: required (DPT 1)
    - invert: optional boolean
    """
    ga_switch = {"write": address}
    if state_address:
        ga_switch["state"] = state_address

    knx_config = {"ga_switch": ga_switch}
    if invert:
        knx_config["invert"] = True

    return {
        "entity": {"name": name},
        "knx": knx_config,
    }


def get_binary_sensor_config(name, state_address, device_class=None):
    """Build a `binary_sensor` entity payload.

    Per the live KNX schema (knx/get_schema platform=binary_sensor), ga_sensor
    is read-only: it requires `state` and rejects `write`. `device_class` is
    a generic entity-registry field and lives under `entity`, not `knx`.

    - knx.ga_sensor: required (DPT 1), uses `state`
    - entity.device_class: optional (motion, door, window, etc.)
    """
    entity = {"name": name}
    if device_class:
        entity["device_class"] = device_class

    return {
        "entity": entity,
        "knx": {"ga_sensor": {"state": state_address}},
    }


def get_climate_config(
    name,
    temperature_state,
    setpoint_write=None,
    setpoint_state=None,
    operation_mode_write=None,
    operation_mode_state=None,
    on_off_write=None,
    on_off_state=None,
    controller_mode="heat",
):
    """Build a `climate` (thermostat) entity payload.

    Verified against knx/get_schema for platform "climate":
    - ga_temperature_current: required (DPT 9.001), state-only (read from bus)
    - target_temperature: group select with ga_temperature_target write+state
      (DPT 9.001)
    - ga_operation_mode: optional (DPT 20.102)
    - default_controller_mode: required; one of CLIMATE_CONTROLLER_MODES

    Args:
        name: Entity name.
        temperature_state: Current temperature state address (e.g., "1/6/0").
        setpoint_write: Setpoint write address (e.g., "1/6/3"). If omitted, a
            read-only thermostat is produced (no target_temperature block) —
            never alias a write address onto the temperature sensor GA.
        setpoint_state: Setpoint state address (optional, e.g., "1/6/4").
        operation_mode_write: Operation mode write address (optional, DPT 20.102).
        operation_mode_state: Operation mode state address (optional).
        on_off_write: On/off write address (optional, DPT 1).
        on_off_state: On/off state address (optional).
        controller_mode: Default HVAC controller mode. Must be one of
            CLIMATE_CONTROLLER_MODES. Defaults to "heat".
    """
    if controller_mode not in CLIMATE_CONTROLLER_MODES:
        raise ValueError(
            f"controller_mode must be one of {CLIMATE_CONTROLLER_MODES}, "
            f"got {controller_mode!r}"
        )

    knx_config = {
        "ga_temperature_current": {"state": temperature_state},
        "default_controller_mode": controller_mode,
    }
    if setpoint_write:
        target_temperature = {"ga_temperature_target": {"write": setpoint_write}}
        if setpoint_state:
            target_temperature["ga_temperature_target"]["state"] = setpoint_state
        knx_config["target_temperature"] = target_temperature
    if operation_mode_write:
        entry = {"write": operation_mode_write}
        if operation_mode_state:
            entry["state"] = operation_mode_state
        knx_config["ga_operation_mode"] = entry
    if on_off_write:
        entry = {"write": on_off_write}
        if on_off_state:
            entry["state"] = on_off_state
        knx_config["ga_on_off"] = entry

    return {
        "entity": {"name": name},
        "knx": knx_config,
    }


def get_cover_config(
    name,
    up_down_write,
    stop_write=None,
    position_set_write=None,
    position_state=None,
    travelling_time_up=60,
    travelling_time_down=60,
):
    """Build a `cover` entity payload."""
    knx_config = {
        "ga_up_down": {"write": up_down_write},
        "travelling_time_up": travelling_time_up,
        "travelling_time_down": travelling_time_down,
    }
    if stop_write:
        knx_config["ga_stop"] = {"write": stop_write}
    if position_set_write:
        knx_config["ga_position_set"] = {"write": position_set_write}
    if position_state:
        knx_config["ga_position_state"] = {"state": position_state}
    return {"entity": {"name": name}, "knx": knx_config}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser():
    parser = argparse.ArgumentParser(
        description="KNX Entity Creator via Home Assistant WebSocket API.",
    )
    parser.add_argument(
        "--url",
        default="ws://localhost:8123/api/websocket",
        help="Home Assistant WebSocket URL (default: %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Long-lived access token. If omitted, falls back to the "
        "HA_TOKEN environment variable.",
    )
    parser.add_argument(
        "--entity-type",
        choices=["light", "switch", "binary_sensor", "climate", "cover"],
        help="Type of entity to create. Required unless --delete is used.",
    )
    parser.add_argument("--name", help="Display name for the new entity.")
    parser.add_argument(
        "--address",
        help="Primary KNX group address. Meaning depends on --entity-type: "
        "switch/light = on/off write; binary_sensor = state; "
        "cover = up/down write.",
    )
    parser.add_argument(
        "--state-address", help="State feedback address (light/switch)."
    )
    parser.add_argument(
        "--brightness-address", help="Brightness write address (light)."
    )
    parser.add_argument(
        "--brightness-state-address", help="Brightness state address (light)."
    )
    parser.add_argument("--color-address", help="Color write address (light).")
    parser.add_argument("--color-state-address", help="Color state address (light).")
    parser.add_argument(
        "--color-dpt",
        help='DPT for color address: "251.600" RGBW, "232.600" RGB, "242.600" xyY.',
    )
    parser.add_argument("--invert", action="store_true", help="Invert switch state.")
    parser.add_argument("--device-class", help="Device class (binary_sensor).")
    parser.add_argument(
        "--temperature-state", help="Current temperature state (climate)."
    )
    parser.add_argument("--setpoint-write", help="Setpoint write address (climate).")
    parser.add_argument("--setpoint-state", help="Setpoint state address (climate).")
    parser.add_argument(
        "--operation-mode-write", help="Operation mode write (climate)."
    )
    parser.add_argument(
        "--operation-mode-state", help="Operation mode state (climate)."
    )
    parser.add_argument("--on-off-write", help="On/off write address (climate).")
    parser.add_argument("--on-off-state", help="On/off state address (climate).")
    parser.add_argument(
        "--controller-mode",
        choices=CLIMATE_CONTROLLER_MODES,
        default="heat",
        help="Default HVAC controller mode for climate entities (default: %(default)s).",
    )
    parser.add_argument("--stop-write", help="Stop write address (cover).")
    parser.add_argument("--position-set-write", help="Position set write (cover).")
    parser.add_argument("--position-state", help="Position state address (cover).")
    parser.add_argument(
        "--travelling-time-up",
        type=int,
        default=60,
        help="Travelling time up in seconds (cover, default: 60).",
    )
    parser.add_argument(
        "--travelling-time-down",
        type=int,
        default=60,
        help="Travelling time down in seconds (cover, default: 60).",
    )
    parser.add_argument(
        "--delete",
        help="Delete entity by entity_id (e.g., light.test_light). "
        "Mutually exclusive with creation flags.",
    )
    return parser


def _resolve_token(args):
    """Pick token from --token, falling back to $HA_TOKEN."""
    token = args.token or os.environ.get("HA_TOKEN")
    if not token:
        sys.exit(
            "error: a token is required. Pass --token or set the HA_TOKEN "
            "environment variable."
        )
    return token


def _build_payload(args):
    """Translate CLI args into (platform, payload). Validates required args."""
    if not args.entity_type:
        sys.exit("error: --entity-type is required (or use --delete).")
    if not args.name:
        sys.exit("error: --name is required.")

    et = args.entity_type
    if et == "light":
        if not args.address:
            sys.exit("error: --address is required for light.")
        return et, get_light_config(
            name=args.name,
            address=args.address,
            state_address=args.state_address,
            brightness_address=args.brightness_address,
            brightness_state_address=args.brightness_state_address,
            color_address=args.color_address,
            color_state_address=args.color_state_address,
            color_dpt=args.color_dpt,
        )
    if et == "switch":
        if not args.address:
            sys.exit("error: --address is required for switch.")
        return et, get_switch_config(
            name=args.name,
            address=args.address,
            state_address=args.state_address,
            invert=args.invert,
        )
    if et == "binary_sensor":
        if not args.address:
            sys.exit("error: --address is required for binary_sensor.")
        return et, get_binary_sensor_config(
            name=args.name,
            state_address=args.address,
            device_class=args.device_class,
        )
    if et == "climate":
        missing = [
            flag
            for flag, val in (
                ("--temperature-state", args.temperature_state),
                ("--setpoint-write", args.setpoint_write),
            )
            if not val
        ]
        if missing:
            sys.exit(f"error: {', '.join(missing)} required for climate.")
        return et, get_climate_config(
            name=args.name,
            temperature_state=args.temperature_state,
            setpoint_write=args.setpoint_write,
            setpoint_state=args.setpoint_state,
            operation_mode_write=args.operation_mode_write,
            operation_mode_state=args.operation_mode_state,
            on_off_write=args.on_off_write,
            on_off_state=args.on_off_state,
            controller_mode=args.controller_mode,
        )
    if et == "cover":
        if not args.address:
            sys.exit("error: --address is required for cover (up/down write).")
        return et, get_cover_config(
            name=args.name,
            up_down_write=args.address,
            stop_write=args.stop_write,
            position_set_write=args.position_set_write,
            position_state=args.position_state,
            travelling_time_up=args.travelling_time_up,
            travelling_time_down=args.travelling_time_down,
        )
    # argparse `choices` should prevent reaching here.
    sys.exit(f"error: unsupported entity type {et!r}")


async def _run_delete(args, token):
    print(f"Connecting to {args.url}...")
    websocket = await connect_and_authenticate(args.url, token)
    try:
        msg_id = 1
        entity_id = args.delete

        # 1. Remove from KNX config store
        print(f"\n--- Removing from KNX config: {entity_id} ---")
        knx_result = await delete_entity(websocket, msg_id, entity_id)
        msg_id += 1
        knx_ok = knx_result.get("success", False)
        print(f"  KNX config: {'removed' if knx_ok else 'not found or failed'}")

        # 2. Remove from entity registry (prevents orphaned entries)
        print(f"\n--- Removing from entity registry: {entity_id} ---")
        reg_result = await remove_entity_registry_entry(websocket, msg_id, entity_id)
        reg_ok = reg_result.get("success", False)
        print(f"  Entity registry: {'removed' if reg_ok else 'not found or failed'}")

        if knx_ok or reg_ok:
            print(f"\nEntity {entity_id!r} removed from HA.")
        else:
            print(f"\nEntity {entity_id!r} not found in either store.")
    finally:
        await websocket.close()
        print("Connection closed.")


async def _run_create(args, token):
    platform, data = _build_payload(args)

    print(f"Connecting to {args.url}...")
    websocket = await connect_and_authenticate(args.url, token)
    try:
        msg_id = 1

        print("\n--- Getting KNX base data ---")
        base_data = await get_knx_base_data(websocket, msg_id)
        msg_id += 1
        print(
            "Supported platforms: "
            f"{base_data.get('result', {}).get('supported_platforms', [])}"
        )

        print("\n--- Getting current entity entries ---")
        entries = await get_entity_entries(websocket, msg_id)
        msg_id += 1
        existing = entries.get("result", [])
        print(f"Currently configured entities: {len(existing)}")

        print(f"\n--- Validating {platform} entity ---")
        print(f"Data: {json.dumps(data, indent=2)}")
        validate_result = await validate_entity(websocket, msg_id, platform, data)
        msg_id += 1
        print(f"Validation: {json.dumps(validate_result, indent=2)}")

        # The WS envelope's top-level `success` only confirms the call
        # round-tripped; the actual validation verdict lives in result.success.
        if not (
            validate_result.get("success")
            and validate_result.get("result", {}).get("success")
        ):
            print("\nValidation failed. Entity not created.")
            return

        print(f"\n--- Creating {platform} entity ---")
        create_result = await create_entity(websocket, msg_id, platform, data)
        print(f"Creation: {json.dumps(create_result, indent=2)}")

        print("\n--- Done ---")
    finally:
        await websocket.close()
        print("Connection closed.")


async def main():
    args = _build_arg_parser().parse_args()
    token = _resolve_token(args)

    if args.delete:
        await _run_delete(args, token)
    else:
        await _run_create(args, token)


if __name__ == "__main__":
    asyncio.run(main())
