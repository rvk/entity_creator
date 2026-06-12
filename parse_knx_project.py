#!/usr/bin/env python3
"""
KNX Project Parser — Parse ETS .knxproj files and create Home Assistant KNX entities.

Uses xknxproject to extract group addresses, devices, and functions from a KNX
project file, then determines appropriate Home Assistant entity types and
optionally creates them via entity_creator's WebSocket API.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from xknxproject import XKNXProj

# Import entity_creator functions
from update_knx_config import (
    connect_and_authenticate,
    create_entity,
    get_entity_entries,
    validate_entity,
    get_light_config,
    get_switch_config,
    get_binary_sensor_config,
    get_climate_config,
    get_cover_config,
    CLIMATE_CONTROLLER_MODES,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Map KNX function types to HA entity platforms
FUNCTION_TYPE_MAP: Dict[str, Dict[str, str]] = {
    "FT-1": {"platform": "light", "usage": "switchable light"},
    "FT-6": {"platform": "light", "usage": "dimmable light"},
    "FT-8": {"platform": "climate", "usage": "heating"},
    "FT-10": {"platform": "switch", "usage": "switchable socket"},
    # FT-0 is custom — need heuristic analysis
}

# DPT main number to descriptive name
DPT_NAMES: Dict[int, str] = {
    1: "1.xxx (1-bit)",
    3: "3.xxx (4-bit dimming)",
    5: "5.xxx (1-byte scaling)",
    7: "7.xxx (2-byte unsigned)",
    9: "9.xxx (2-byte float)",
    10: "10.xxx (time)",
    11: "11.xxx (date)",
    13: "13.xxx (4-byte)",
    14: "14.xxx (4-byte float)",
    18: "18.xxx (scene)",
    20: "20.xxx (HVAC)",
    27: "27.xxx (combined info)",
}


def parse_knx_project(filepath: str, password: str = "") -> dict:
    """Parse a .knxproj file using xknxproject.

    Args:
        filepath: Path to the .knxproj file.
        password: Project password (empty string if not protected).

    Returns:
        The parsed project dict from XKNXProj.parse().
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"KNX project file not found: {filepath}")

    proj = XKNXProj(path=filepath, password=password)
    return proj.parse()


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------


def _clean_entity_name(raw: str) -> str:
    """Clean a KNX group address or function name into an entity name."""
    name = raw.strip()
    # Remove ETS channel markers like {0}, {1} — universal KNX convention
    name = re.sub(r"\{\d+\}", "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    name = name.strip()
    return name


def _determine_flags_role(ga_data: dict, com_objects: dict) -> Optional[str]:
    """Determine GA role from communication object flags.

    Returns 'write', 'state', 'both', or None.
    """
    roles = set()
    for co_id in ga_data.get("communication_object_ids", []):
        co = com_objects.get(co_id, {})
        flags = co.get("flags", {})
        if flags.get("write"):
            roles.add("write")
        if flags.get("transmit") and not flags.get("write"):
            roles.add("state")
        if flags.get("read") and not flags.get("write") and not flags.get("transmit"):
            roles.add("state")

    if "write" in roles and "state" in roles:
        return "both"
    if "write" in roles:
        return "write"
    if "state" in roles:
        return "state"
    return None


def _classify_com_object(co: dict) -> Dict[str, Any]:
    """Classify a single com object by its DPTs and flags only.

    No manufacturer-specific names, no language-specific text.
    """
    flags = co.get("flags", {})
    write = flags.get("write", False)
    transmit = flags.get("transmit", False)
    read = flags.get("read", False)
    size = co.get("object_size", "")

    dpt_mains = {d.get("main") for d in co.get("dpts", []) if d.get("main")}

    return {
        "dpt_mains": dpt_mains,
        "size": size,
        "is_pure_write": write and not transmit and not read,
        "is_write_only": write and not transmit,
        "is_transmit_only": transmit and not write,
        "is_write_transmit": write and transmit,
        "is_write_read": write and read and not transmit,
        "has_dpt1": 1 in dpt_mains,
        "has_dpt9": 9 in dpt_mains,
        "has_dpt5": 5 in dpt_mains,
        "has_dpt3": 3 in dpt_mains,
    }


def _classify_device(dev: dict, com_objects: dict) -> Dict[str, Any]:
    """Classify a device by analyzing ALL its com objects' DPTs and flags.

    A device can be multiple things at once (e.g., a RailQUAD has
    temperature probes AND thermostat logic AND binary inputs).
    We classify by what the device DOES:
      - pure_actuator: has DPT 1 write-only (accepts bus commands)
      - lighting_gateway: has DPT 5 or DPT 3
      - sensor_input: DPT 1 transmit (originates sensor readings),
        NOT write-only DPT 1
      - thermostat_controller: has DPT 9 AND write-capable DPT 1
        (can command something), not just DPT 9 read-only
    """
    co_ids = dev.get("communication_object_ids", [])
    cos = []
    for co_id in co_ids:
        co = com_objects.get(co_id)
        if co:
            cos.append(_classify_com_object(co))

    if not cos:
        return {
            "has_any_dpt9": False,
            "has_any_dpt5": False,
            "has_any_dpt3": False,
            "has_write_only_dpt1": False,
            "has_pure_write_dpt1": False,
            "has_transmit_dpt1": False,
            "is_pure_actuator": False,
            "is_lighting_gateway": False,
            "is_sensor_input": False,
            "is_thermostat_controller": False,
        }

    has_any_dpt9 = any(c["has_dpt9"] for c in cos)
    has_any_dpt5 = any(c["has_dpt5"] for c in cos)
    has_any_dpt3 = any(c["has_dpt3"] for c in cos)
    has_write_only_dpt1 = any(c["has_dpt1"] and c["is_write_only"] for c in cos)
    has_pure_write_dpt1 = any(c["has_dpt1"] and c["is_pure_write"] for c in cos)
    has_transmit_dpt1 = any(c["has_dpt1"] and c["is_transmit_only"] for c in cos)
    has_write_transmit_dpt1 = any(c["has_dpt1"] and c["is_write_transmit"] for c in cos)
    has_write_read_dpt1 = any(c["has_dpt1"] and c["is_write_read"] for c in cos)

    # Pure actuator: accepts DPT 1 write-only commands
    is_pure_actuator = has_pure_write_dpt1

    # Lighting gateway: has DPT 5 (absolute brightness) or DPT 3 (dimming)
    is_lighting_gateway = has_any_dpt5 or has_any_dpt3

    # Sensor input: originates DPT 1 sensor readings (transmit or WTR),
    # but does NOT accept write-only DPT 1 commands
    is_sensor_input = (
        has_transmit_dpt1 or has_write_transmit_dpt1
    ) and not has_pure_write_dpt1

    # Thermostat controller: has DPT 9 AND can issue DPT 1 commands
    # (write-only, write+transmit, or write+read DPT 1 on the same device).
    # A device with DPT 9 probes and WTR DPT 1 sensors only is NOT a
    # thermostat controller — it's a sensor input.
    is_thermostat_controller = has_any_dpt9 and (
        has_pure_write_dpt1 or has_write_transmit_dpt1 or has_write_read_dpt1
    )

    return {
        "has_any_dpt9": has_any_dpt9,
        "has_any_dpt5": has_any_dpt5,
        "has_any_dpt3": has_any_dpt3,
        "has_write_only_dpt1": has_write_only_dpt1,
        "has_pure_write_dpt1": has_pure_write_dpt1,
        "has_transmit_dpt1": has_transmit_dpt1,
        "is_pure_actuator": is_pure_actuator,
        "is_lighting_gateway": is_lighting_gateway,
        "is_sensor_input": is_sensor_input,
        "is_thermostat_controller": is_thermostat_controller,
    }


def _classify_ga(ga_data: dict, com_objects: dict, devices: dict) -> Dict[str, Any]:
    """Classify a GA by its DPT, com-object flags, and device role(s).

    Uses only KNX-level data. No manufacturer names, no language text.
    """
    dpt = ga_data.get("dpt") or {}
    co_ids = ga_data.get("communication_object_ids", [])
    cos = []
    for co_id in co_ids:
        co = com_objects.get(co_id)
        if co:
            cos.append(_classify_com_object(co))

    result = {
        "dpt_main": dpt.get("main"),
        "dpt_sub": dpt.get("sub"),
        "has_write": False,
        "has_transmit": False,
        "is_pure_actuator": False,
        "is_lighting_gateway": False,
        "is_sensor_input": False,
        "is_thermostat_controller": False,
        "com_count": len(cos),
    }

    for co in cos:
        if co["is_write_only"] or co["is_write_transmit"] or co["is_write_read"]:
            result["has_write"] = True
        if co["is_transmit_only"]:
            result["has_transmit"] = True

    # Device-level classification — aggregate across all linked devices
    for co_id in co_ids:
        co = com_objects.get(co_id, {})
        dev_addr = co.get("device_address", "")
        dev = devices.get(dev_addr, {})
        if not dev:
            continue
        drole = _classify_device(dev, com_objects)
        if drole["is_pure_actuator"]:
            result["is_pure_actuator"] = True
        if drole["is_lighting_gateway"]:
            result["is_lighting_gateway"] = True
        if drole["is_sensor_input"]:
            result["is_sensor_input"] = True
        if drole["is_thermostat_controller"]:
            result["is_thermostat_controller"] = True

    return result


def _extract_from_functions(
    project: dict,
) -> List[Dict[str, Any]]:
    """Extract entities from KNX functions (the most reliable source).

    Each function groups related GAs and specifies a function type.
    """
    functions = project.get("functions", {})
    group_addresses = project.get("group_addresses", {})
    com_objects = project.get("communication_objects", {})
    locations = project.get("locations", {})

    entities: List[Dict[str, Any]] = []

    for fn_id, fn in functions.items():
        fn_type = fn.get("function_type", "")
        fn_name = fn.get("name", fn_id)
        fn_gas = fn.get("group_addresses", {})
        usage = fn.get("usage_text", "")

        if not fn_gas:
            continue

        # Determine platform from function type
        mapping = FUNCTION_TYPE_MAP.get(fn_type)
        if mapping:
            platform = mapping["platform"]
        else:
            # FT-0 or unknown — use heuristics
            platform = _heuristic_platform(fn_gas, group_addresses, com_objects)

        entity_name = _clean_entity_name(fn_name)

        # Get location/space name
        space_id = fn.get("space_id", "")
        space_name = _resolve_space_name(space_id, locations)

        # Build entity based on platform
        entity_data = _build_entity_for_platform(
            platform=platform,
            entity_name=entity_name,
            ga_keys=list(fn_gas.keys()),
            group_addresses=group_addresses,
            com_objects=com_objects,
            function_type=fn_type,
            devices=project.get("devices", {}),
        )

        if entity_data:
            entity_data["_meta"] = {
                "function_id": fn_id,
                "function_type": fn_type,
                "function_name": fn_name,
                "usage_text": usage,
                "space": space_name,
                "space_id": space_id,
            }
            entities.append(entity_data)

    return entities


def _extract_unmapped(
    project: dict,
    mapped_addresses: set,
) -> List[Dict[str, Any]]:
    """Extract entities from GAs not covered by any function.

    Uses DPT + com-object flags + device type (not naming conventions).
    """
    group_addresses = project.get("group_addresses", {})
    com_objects = project.get("communication_objects", {})
    devices = project.get("devices", {})
    entities = []
    consumed = set()

    # --- Step 1: Pair unmapped relay switch-write + status-feedback ---
    unmapped_actuator = []
    for addr, ga in group_addresses.items():
        if addr in mapped_addresses:
            continue
        cls = _classify_ga(ga, com_objects, devices)
        if cls["dpt_main"] != 1:
            continue
        if not cls["is_pure_actuator"]:
            continue
        unmapped_actuator.append((addr, ga))

    unmapped_actuator.sort(key=lambda x: x[1].get("raw_address", 0))

    i = 0
    while i < len(unmapped_actuator) - 1:
        addr_a, ga_a = unmapped_actuator[i]
        addr_b, ga_b = unmapped_actuator[i + 1]
        raw_a = ga_a.get("raw_address", 0)
        raw_b = ga_b.get("raw_address", 0)

        if raw_b - raw_a == 1:
            cls_a = _classify_ga(ga_a, com_objects, devices)
            cls_b = _classify_ga(ga_b, com_objects, devices)

            a_is_write = cls_a["has_write"] and not cls_a["has_transmit"]
            a_is_state = cls_a["has_transmit"] and not cls_a["has_write"]
            b_is_write = cls_b["has_write"] and not cls_b["has_transmit"]
            b_is_state = cls_b["has_transmit"] and not cls_b["has_write"]

            if a_is_write and b_is_state:
                switch_write, switch_state = addr_a, addr_b
            elif b_is_write and a_is_state:
                switch_write, switch_state = addr_b, addr_a
            elif a_is_write and not b_is_write:
                switch_write, switch_state = addr_a, addr_b
            elif b_is_write and not a_is_write:
                switch_write, switch_state = addr_b, addr_a
            else:
                i += 1
                continue

            name = _clean_entity_name(ga_a["name"])
            entity_data = get_switch_config(
                name=name, address=switch_write, state_address=switch_state
            )
            entity_data["_meta"] = {
                "function_id": None,
                "function_type": None,
                "function_name": name,
                "usage_text": "unmapped relay channel",
                "space": "",
                "space_id": "",
            }
            entities.append(entity_data)
            consumed.update([addr_a, addr_b])
            i += 2
        else:
            i += 1

    # --- Step 2: Binary sensor inputs ---
    for addr, ga in group_addresses.items():
        if addr in mapped_addresses or addr in consumed:
            continue
        cls = _classify_ga(ga, com_objects, devices)
        if cls["dpt_main"] != 1:
            continue
        if cls["is_sensor_input"] and not cls["is_pure_actuator"]:
            name = _clean_entity_name(ga["name"]) or f"Binary {addr}"
            entity_data = get_binary_sensor_config(name=name, state_address=addr)
            entity_data["_meta"] = {
                "function_id": None,
                "function_type": None,
                "function_name": ga["name"],
                "usage_text": "binary sensor input",
                "space": "",
                "space_id": "",
            }
            entities.append(entity_data)
            consumed.add(addr)

    return entities


def _build_entity_for_platform(
    platform: str,
    entity_name: str,
    ga_keys: List[str],
    group_addresses: dict,
    com_objects: dict,
    function_type: str,
    devices: dict,
) -> Optional[Dict[str, Any]]:
    """Build an entity payload for the given platform using available GAs."""
    gas = [(k, group_addresses.get(k, {})) for k in ga_keys]

    if platform == "light":
        return _build_light(entity_name, gas, com_objects, function_type, devices)
    elif platform == "switch":
        return _build_switch(entity_name, gas, com_objects, devices)
    elif platform == "climate":
        return _build_climate(entity_name, gas, com_objects, group_addresses, devices)
    elif platform == "binary_sensor":
        return _build_binary_sensor(entity_name, gas)
    elif platform == "cover":
        return _build_cover(entity_name, gas)

    return None


def _build_light(
    name: str,
    gas: List[Tuple[str, dict]],
    com_objects: dict,
    function_type: str,
    devices: dict = None,
) -> Optional[Dict[str, Any]]:
    """Build a light entity from a group of GAs using DPT + flags."""
    if devices is None:
        devices = {}

    switch_write = None
    switch_state = None
    brightness_write = None
    brightness_state = None

    for addr, ga in gas:
        cls = _classify_ga(ga, com_objects, devices)
        dpt = cls["dpt_main"]

        if dpt == 1:
            if cls["has_write"] and not cls["has_transmit"]:
                switch_write = addr
            elif cls["has_transmit"] and not cls["has_write"]:
                switch_state = addr
            elif cls["has_write"]:
                switch_write = addr
            elif cls["has_transmit"]:
                switch_state = addr
        elif dpt == 5:
            if cls["has_write"] and not cls["has_transmit"]:
                brightness_write = addr
            elif cls["has_transmit"] and not cls["has_write"]:
                brightness_state = addr
            elif cls["has_write"]:
                brightness_write = addr
            elif cls["has_transmit"]:
                brightness_state = addr
        elif dpt == 3:
            pass

    if switch_write is None:
        return None

    return get_light_config(
        name=name,
        address=switch_write,
        state_address=switch_state,
        brightness_address=brightness_write,
        brightness_state_address=brightness_state,
    )


def _build_switch(
    name: str,
    gas: List[Tuple[str, dict]],
    com_objects: dict,
    devices: dict = None,
) -> Optional[Dict[str, Any]]:
    """Build a switch entity from a group of GAs using DPT + flags."""
    if devices is None:
        devices = {}

    switch_write = None
    switch_state = None

    for addr, ga in gas:
        cls = _classify_ga(ga, com_objects, devices)
        if cls["dpt_main"] != 1:
            continue

        if cls["has_write"] and not cls["has_transmit"]:
            switch_write = addr
        elif cls["has_transmit"] and not cls["has_write"]:
            switch_state = addr
        elif cls["has_write"]:
            switch_write = addr
        elif cls["has_transmit"]:
            switch_state = addr

    if switch_write is None:
        return None

    return get_switch_config(
        name=name,
        address=switch_write,
        state_address=switch_state,
    )


def _build_climate(
    name: str,
    gas: List[Tuple[str, dict]],
    com_objects: dict,
    group_addresses: dict,
    devices: dict = None,
) -> Optional[Dict[str, Any]]:
    """Build a climate entity using DPT + flags + device roles only.

    No manufacturer-specific com-object names. No language-dependent text.

    Classification:
      - DPT 9, transmit-only (no write) → temperature current (probe)
      - DPT 9, write+read → setpoint (target temperature)
      - DPT 1, linked to a pure-actuator device → control variable → SKIP
      - DPT 1, linked only to thermostat-capable devices → on_off control
    """
    if devices is None:
        devices = {}

    temp_state = None
    setpoint_write = None
    on_off_write = None

    for addr, ga in gas:
        dpt = (ga.get("dpt") or {}).get("main")

        if dpt == 9:
            # Aggregate flags across all com objects for this GA
            has_write_9 = False
            has_transmit_9 = False
            for co_id in ga.get("communication_object_ids", []):
                co = com_objects.get(co_id, {})
                cc = _classify_com_object(co)
                if not cc["has_dpt9"]:
                    continue
                if (
                    cc["is_write_only"]
                    or cc["is_write_read"]
                    or cc["is_write_transmit"]
                ):
                    has_write_9 = True
                if cc["is_transmit_only"]:
                    has_transmit_9 = True

            if has_transmit_9 and not has_write_9:
                # Pure transmit: temperature sensor probe value
                temp_state = addr
            elif has_write_9 and not has_transmit_9:
                # Write-only: thermostat reading from sensor
                if not temp_state:
                    temp_state = addr
            elif has_write_9:
                # Write+transmit or write+read: setpoint
                setpoint_write = addr

        elif dpt == 1:
            # Determine if this GA goes to a pure actuator (control variable)
            # or stays on a thermostat (on/off)
            links_to_actuator = False
            links_to_thermostat = False

            for co_id in ga.get("communication_object_ids", []):
                co = com_objects.get(co_id, {})
                cc = _classify_com_object(co)
                if not cc["has_dpt1"]:
                    continue
                dev_addr = co.get("device_address", "")
                dev = devices.get(dev_addr, {})
                drole = _classify_device(dev, com_objects)

                if drole["is_pure_actuator"]:
                    links_to_actuator = True
                if drole["is_thermostat_controller"]:
                    links_to_thermostat = True

            if links_to_actuator:
                # Control variable output (thermostat → actuator relay) — skip
                continue
            if links_to_thermostat and not links_to_actuator:
                # On/Off command on a thermostat-capable device
                if on_off_write is None:
                    on_off_write = addr

    if temp_state is None:
        return None

    return get_climate_config(
        name=name,
        temperature_state=temp_state,
        setpoint_write=setpoint_write or temp_state,
        on_off_write=on_off_write,
        controller_mode="heat",
    )


def _build_binary_sensor(
    name: str, gas: List[Tuple[str, dict]]
) -> Optional[Dict[str, Any]]:
    """Build a binary_sensor entity from GAs."""
    if not gas:
        return None
    state_address = gas[0][0]
    return get_binary_sensor_config(name=name, state_address=state_address)


def _build_cover(name: str, gas: List[Tuple[str, dict]]) -> Optional[Dict[str, Any]]:
    """Build a cover entity. Uses DPT + address ordering as fallback.

    Covers typically use DPT 1.008 (up/down) and DPT 1.007 (stop).
    Without DPT info, falls back to address ordering.
    """
    up_down = None
    stop_write = None
    position = None

    for addr, ga in gas:
        dpt = ga.get("dpt", {})
        dpt_main = dpt.get("main") if dpt else None
        dpt_sub = dpt.get("sub") if dpt else None

        # DPT 1.008 = up/down, DPT 1.007 = step/stop
        if dpt_main == 1 and dpt_sub == 8:
            up_down = addr
        elif dpt_main == 1 and dpt_sub == 7:
            stop_write = addr
        elif dpt_main == 5:
            position = addr
        elif up_down is None:
            up_down = addr  # fallback: first GA

    if up_down is None:
        return None

    return get_cover_config(
        name=name,
        up_down_write=up_down,
        stop_write=stop_write,
        position_set_write=position,
    )


def _heuristic_platform(
    fn_gas: dict,
    group_addresses: dict,
    com_objects: dict,
) -> str:
    """Guess the platform for unknown function types (FT-0 etc.)."""
    dpt_mains = set()
    for addr in fn_gas:
        ga = group_addresses.get(addr, {})
        dpt = ga.get("dpt")
        if dpt:
            dpt_mains.add(dpt["main"])
        for co_id in ga.get("communication_object_ids", []):
            co = com_objects.get(co_id, {})
            for d in co.get("dpts", []):
                dpt_mains.add(d["main"])

    # If all are DPT 1 → switch
    if dpt_mains == {1}:
        return "switch"
    # If DPT 9 present → climate
    if 9 in dpt_mains:
        return "climate"
    # If DPT 3 or 5 present → light (dimming)
    if dpt_mains & {3, 5}:
        return "light"
    # Default
    return "switch"


def _resolve_space_name(space_id: str, locations: dict) -> str:
    """Find a space name by its identifier."""
    if not space_id:
        return ""

    def _search(spaces: dict, target: str) -> Optional[str]:
        for name, space in spaces.items():
            if space.get("identifier") == target:
                return name
            result = _search(space.get("spaces", {}), target)
            if result:
                return result
        return None

    return _search(locations, space_id) or ""


# ---------------------------------------------------------------------------
# Entity extraction (main entry point)
# ---------------------------------------------------------------------------


def extract_entities(project: dict) -> List[Dict[str, Any]]:
    """Extract all Home Assistant entities from a parsed KNX project.

    Returns a list of entity payloads (same format as update_knx_config builders).
    Each payload has an added ``_meta`` key with KNX-specific metadata.
    """
    entities = []

    # 1. Extract from functions (most reliable)
    function_entities = _extract_from_functions(project)
    entities.extend(function_entities)

    # Track which GAs were already mapped
    mapped = set()
    for ent in function_entities:
        meta = ent.get("_meta", {})
        fn_id = meta.get("function_id")
        if fn_id:
            fn = project.get("functions", {}).get(fn_id, {})
            for addr in fn.get("group_addresses", {}):
                mapped.add(addr)

    # 2. Extract unmapped GAs (binary inputs, etc.)
    unmapped = _extract_unmapped(project, mapped)
    entities.extend(unmapped)

    return entities


# ---------------------------------------------------------------------------
# Output / reporting
# ---------------------------------------------------------------------------


def print_entity_summary(entities: List[Dict[str, Any]]) -> None:
    """Print a human-readable summary of extracted entities."""
    print(f"\n{'=' * 70}")
    print(f"  Extracted {len(entities)} entities from KNX project")
    print(f"{'=' * 70}")

    by_platform = defaultdict(list)
    for ent in entities:
        meta = ent.get("_meta", {})
        platform = meta.get("platform", "?")
        by_platform[platform].append(ent)

    for platform, ents in sorted(by_platform.items()):
        print(f"\n--- {platform.upper()} ({len(ents)} entities) ---")
        for ent in ents:
            meta = ent.get("_meta", {})
            name = ent.get("entity", {}).get("name", "?")
            fn_type = meta.get("function_type", "")
            space = meta.get("space", "")
            knx = ent.get("knx", {})

            # Build a short address summary
            addr_parts = []
            for key, val in knx.items():
                if isinstance(val, dict):
                    if "write" in val:
                        addr_parts.append(f"{key}={val['write']}")
                    elif "state" in val:
                        addr_parts.append(f"{key}={val['state']}")
                elif key == "color" and isinstance(val, dict):
                    cga = val.get("ga_color", {})
                    if "write" in cga:
                        addr_parts.append(f"color={cga['write']}")

            extra = f"  [{fn_type}]" if fn_type else ""
            loc = f" @ {space}" if space else ""
            addrs = f"  ({', '.join(addr_parts)})" if addr_parts else ""
            print(f"  {name}{extra}{loc}{addrs}")


def entities_to_json(entities: List[Dict[str, Any]], include_meta: bool = True) -> str:
    """Serialize entities to JSON string."""
    if include_meta:
        return json.dumps(entities, indent=2, ensure_ascii=False, default=str)
    # Strip _meta for clean output
    clean = []
    for ent in entities:
        clean.append({k: v for k, v in ent.items() if k != "_meta"})
    return json.dumps(clean, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# WebSocket integration (uses entity_creator)
# ---------------------------------------------------------------------------


async def create_entities_batch(
    entities: List[Dict[str, Any]],
    url: str,
    token: str,
    dry_run: bool = True,
    skip_existing: bool = True,
    filter_platform: Optional[str] = None,
    filter_location: Optional[str] = None,
) -> Dict[str, Any]:
    """Create multiple entities via the HA WebSocket API.

    Args:
        entities: List of entity payloads (with _meta).
        url: HA WebSocket URL.
        token: Long-lived access token.
        dry_run: If True, only print what would be created.
        skip_existing: If True, skip entities already in HA.
        filter_platform: Only process entities of this platform.
        filter_location: Only process entities in this location/space.

    Returns:
        Summary dict with success/failure counts.
    """
    # Filter entities
    filtered = []
    for ent in entities:
        meta = ent.get("_meta", {})
        platform = meta.get("platform", "?")
        space = meta.get("space", "")

        if filter_platform and platform != filter_platform:
            continue
        if filter_location and space != filter_location:
            continue
        filtered.append(ent)

    if not filtered:
        print("No entities match the filters.")
        return {"total": 0, "created": 0, "skipped": 0, "failed": 0}

    print(f"\nProcessing {len(filtered)} entities (dry_run={dry_run})...")

    if dry_run:
        print_entity_summary(filtered)
        print(f"\n[Dry run] Would create {len(filtered)} entities.")
        return {"total": len(filtered), "created": 0, "skipped": 0, "failed": 0}

    # Connect and authenticate
    print(f"Connecting to {url}...")
    websocket = await connect_and_authenticate(url, token)

    summary = {"total": len(filtered), "created": 0, "skipped": 0, "failed": 0}

    try:
        # Get existing entities for skip_existing
        existing_names = set()
        if skip_existing:
            entries_result = await get_entity_entries(websocket, 1)
            existing = entries_result.get("result", [])
            existing_names = {
                e.get("entity", e.get("entity_id", "")).split(".")[-1] for e in existing
            }

        msg_id = 10  # Start higher to avoid conflicts

        for i, ent in enumerate(filtered):
            meta = ent.get("_meta", {})
            platform = meta.get("platform", "?")
            entity_name = ent.get("entity", {}).get("name", "unknown")
            fn_name = meta.get("function_name", "")

            # Strip _meta before sending
            payload = {k: v for k, v in ent.items() if k != "_meta"}

            # Check existing
            if (
                skip_existing
                and entity_name.lower().replace(" ", "_") in existing_names
            ):
                print(f"  [{i + 1}/{len(filtered)}] SKIP (existing): {entity_name}")
                summary["skipped"] += 1
                continue

            print(f"  [{i + 1}/{len(filtered)}] Creating {platform}: {entity_name}")

            try:
                # Validate
                val_result = await validate_entity(websocket, msg_id, platform, payload)
                msg_id += 1

                if not (
                    val_result.get("success")
                    and val_result.get("result", {}).get("success")
                ):
                    print(f"    Validation failed: {json.dumps(val_result, indent=4)}")
                    summary["failed"] += 1
                    continue

                # Create
                create_result = await create_entity(
                    websocket, msg_id, platform, payload
                )
                msg_id += 1

                if create_result.get("success"):
                    print(f"    OK")
                    summary["created"] += 1
                else:
                    print(f"    Failed: {json.dumps(create_result, indent=4)}")
                    summary["failed"] += 1

            except Exception as exc:
                print(f"    Error: {exc}")
                summary["failed"] += 1

    finally:
        await websocket.close()
        print("Connection closed.")

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse ETS .knxproj files and create Home Assistant KNX entities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run: show what entities would be created
  python3 parse_knx_project.py --project project.knxproj \\
      --password 'your-ets-password'

  # Create all entities in Home Assistant
  python3 parse_knx_project.py --project project.knxproj \\
      --password 'your-ets-password' --token "$HA_TOKEN" --create

  # Only create light entities
  python3 parse_knx_project.py --project project.knxproj --password pass \\
      --create --filter-type light

  # Export entity configs to JSON
  python3 parse_knx_project.py --project project.knxproj --password pass \\
      --output-json entities.json
        """,
    )

    # Project file options
    parser.add_argument(
        "--project",
        required=True,
        help="Path to the .knxproj project file.",
    )
    parser.add_argument(
        "--password",
        default="",
        help="ETS project password (default: empty string). "
        "Can also be set via KNX_PROJECT_PASSWORD env var.",
    )

    # Output options
    parser.add_argument(
        "--output-json",
        help="Write extracted entity configs to a JSON file.",
    )
    parser.add_argument(
        "--no-meta",
        action="store_true",
        help="Omit _meta fields from JSON output.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output with entity details.",
    )

    # Creation options
    parser.add_argument(
        "--create",
        action="store_true",
        help="Actually create entities in Home Assistant. "
        "Without this flag, performs a dry-run.",
    )
    parser.add_argument(
        "--url",
        default="ws://localhost:8123/api/websocket",
        help="Home Assistant WebSocket URL (default: %(default)s).",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Long-lived access token. Falls back to HA_TOKEN env var.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip entities already configured in Home Assistant.",
    )

    # Filtering options
    parser.add_argument(
        "--filter-type",
        choices=["light", "switch", "binary_sensor", "climate", "cover"],
        help="Only process entities of this type.",
    )
    parser.add_argument(
        "--filter-location",
        help="Only process entities in this location/room.",
    )

    return parser


def _resolve_token(args) -> str:
    token = args.token or os.environ.get("HA_TOKEN")
    if args.create and not token:
        sys.exit(
            "error: a token is required for --create. "
            "Pass --token or set the HA_TOKEN environment variable."
        )
    return token or ""


def _resolve_password(args) -> str:
    return args.password or os.environ.get("KNX_PROJECT_PASSWORD", "")


def main():
    args = _build_arg_parser().parse_args()
    password = _resolve_password(args)

    # Parse the KNX project
    print(f"Parsing KNX project: {args.project}")
    try:
        project = parse_knx_project(args.project, password)
    except Exception as exc:
        sys.exit(f"Failed to parse KNX project: {exc}")

    # Print project info
    info = project.get("info", {})
    print(f"  Project: {info.get('name', '?')} ({info.get('project_id', '?')})")
    print(f"  Created by: {info.get('created_by', '?')} {info.get('tool_version', '')}")
    print(f"  Group address style: {info.get('group_address_style', '?')}")
    print(f"  Group addresses: {len(project.get('group_addresses', {}))}")
    print(f"  Devices: {len(project.get('devices', {}))}")
    print(f"  Functions: {len(project.get('functions', {}))}")
    print(f"  Locations: {len(project.get('locations', {}))}")

    # Extract entities
    entities = extract_entities(project)

    if not entities:
        print("\nNo entities could be extracted from the project.")
        return

    # Add platform to _meta for each entity
    for ent in entities:
        meta = ent.setdefault("_meta", {})
        if "platform" not in meta:
            # Determine platform from entity structure
            knx = ent.get("knx", {})
            if "ga_sensor" in knx:
                meta["platform"] = "binary_sensor"
            elif "ga_up_down" in knx:
                meta["platform"] = "cover"
            elif "ga_temperature_current" in knx:
                meta["platform"] = "climate"
            elif "ga_switch" in knx:
                # Could be light or switch; check for brightness/color
                if "ga_brightness" in knx or "color" in knx or "ga_color_temp" in knx:
                    meta["platform"] = "light"
                else:
                    # Check function type for hint
                    fn_type = meta.get("function_type", "")
                    if fn_type == "FT-1" or fn_type == "FT-6":
                        meta["platform"] = "light"
                    elif fn_type == "FT-10":
                        meta["platform"] = "switch"
                    else:
                        meta["platform"] = "switch"  # default heuristic
            else:
                meta["platform"] = "?"

    # JSON output (always do this first, doesn't conflict with create)
    if args.output_json:
        json_str = entities_to_json(entities, include_meta=not args.no_meta)
        with open(args.output_json, "w", encoding="utf-8") as f:
            f.write(json_str)
        print(f"\nEntity configs written to: {args.output_json}")

    # Print summary
    if args.verbose or not args.create:
        print_entity_summary(entities)

    # Create entities if requested
    if args.create:
        token = _resolve_token(args)
        summary = asyncio.run(
            create_entities_batch(
                entities=entities,
                url=args.url,
                token=token,
                dry_run=False,
                skip_existing=args.skip_existing,
                filter_platform=args.filter_type,
                filter_location=args.filter_location,
            )
        )
        print(
            f"\nDone: {summary['created']} created, "
            f"{summary['skipped']} skipped, "
            f"{summary['failed']} failed"
        )


if __name__ == "__main__":
    main()
