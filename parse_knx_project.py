#!/usr/bin/env python3
"""
KNX Project Parser — Parse ETS .knxproj files and create Home Assistant KNX entities.

Uses xknxproject to extract group addresses, devices, and functions from a KNX
project file, then determines appropriate Home Assistant entity types and
optionally creates them via entity_creator's WebSocket API.
"""

import argparse
import asyncio
import itertools
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
    get_entities_by_group,
    validate_entity,
    get_light_config,
    get_switch_config,
    get_binary_sensor_config,
    get_climate_config,
    get_cover_config,
    list_floors,
    create_floor,
    list_areas,
    create_area,
    update_entity_area,
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

# Human-readable reasons used when a builder cannot produce an entity, keyed
# by the platform that was attempted. Surfaced in the skip report (see main()).
_PLATFORM_SKIP_REASON: Dict[str, str] = {
    "light": "no switch write GA (DPT 1) found",
    "switch": "no switch write GA (DPT 1) found",
    "climate": "no current-temperature GA (DPT 9) found",
    "cover": "no up/down GA found",
    "binary_sensor": "no usable state GA found",
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
    skipped: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Extract entities from KNX functions (the most reliable source).

    Each function groups related GAs and specifies a function type.

    Args:
        project: Parsed xknxproject dict.
        skipped: Optional list; functions that could not be turned into an
            entity are appended as ``{"name", "function_type", "reason"}`` so
            callers can report them instead of dropping them silently.
    """
    functions = project.get("functions", {})
    group_addresses = project.get("group_addresses", {})
    com_objects = project.get("communication_objects", {})
    locations = project.get("locations", {})

    entities: List[Dict[str, Any]] = []

    def _skip(name: str, fn_type: str, reason: str) -> None:
        if skipped is not None:
            skipped.append({"name": name, "function_type": fn_type, "reason": reason})

    for fn_id, fn in functions.items():
        fn_type = fn.get("function_type", "")
        fn_name = fn.get("name", fn_id)
        fn_gas = fn.get("group_addresses", {})
        usage = fn.get("usage_text", "")

        if not fn_gas:
            _skip(fn_name, fn_type, "function has no group addresses")
            continue

        # Determine platform from function type
        mapping = FUNCTION_TYPE_MAP.get(fn_type)
        if mapping:
            platform = mapping["platform"]
        else:
            # FT-0 or unknown — use heuristics
            platform = _heuristic_platform(fn_gas, group_addresses, com_objects)

        entity_name = _clean_entity_name(fn_name)

        # Get location/space name and floor
        space_id = fn.get("space_id", "")
        space_name, floor_name = _resolve_space_with_floor(space_id, locations)

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
                "platform": platform,
                "function_id": fn_id,
                "function_type": fn_type,
                "function_name": fn_name,
                "usage_text": usage,
                "space": space_name,
                "space_id": space_id,
                "floor": floor_name,
            }
            entities.append(entity_data)
        else:
            _skip(
                fn_name,
                fn_type,
                _PLATFORM_SKIP_REASON.get(
                    platform, f"no usable group addresses for platform '{platform}'"
                ),
            )

    return entities


def _switch_role(cls: Dict[str, Any]) -> Optional[str]:
    """Classify a DPT-1 GA as a switch 'write' or 'state' from its flags."""
    if cls["has_write"] and not cls["has_transmit"]:
        return "write"
    if cls["has_transmit"] and not cls["has_write"]:
        return "state"
    if cls["has_write"]:
        return "write"
    if cls["has_transmit"]:
        return "state"
    return None


def _actuator_grouping(
    ga: dict, com_objects: dict
) -> Tuple[Optional[tuple], Optional[int], int]:
    """Return ``(channel_key, order, num_devices)`` for a DPT-1 actuator GA.

    Pairing of a switch-write GA with its status-feedback GA is driven by the
    actuator's channel structure rather than by GA-address adjacency:

    - ``channel_key`` groups GAs that belong to the same actuator channel. It
      is ``(device, channel)`` when ETS exposes channel info, otherwise it
      falls back to ``(device,)`` (group by device).
    - ``order`` is the communication-object number — the device's own channel
      layout order — used to pair the n-th write with the n-th status object.
    - ``num_devices`` is how many distinct devices the GA's DPT-1 objects touch;
      ``> 1`` marks a central/group command (not a single channel), which is
      left to the adjacency fallback instead.
    """
    dev_to_num: Dict[str, int] = {}
    channels: set = set()
    for co_id in ga.get("communication_object_ids", []):
        co = com_objects.get(co_id, {})
        cc = _classify_com_object(co)
        if not cc["has_dpt1"]:
            continue
        dev = co.get("device_address") or ""
        if not dev:
            continue
        num = co.get("number")
        if isinstance(num, int):
            dev_to_num[dev] = min(dev_to_num.get(dev, num), num)
        else:
            dev_to_num.setdefault(dev, 0)
        chan = co.get("channel")
        if chan:
            channels.add((dev, chan))

    if not dev_to_num:
        return None, None, 0

    primary_dev = min(dev_to_num, key=lambda d: dev_to_num[d])
    order = dev_to_num[primary_dev]
    chan_key = next((c for c in channels if c[0] == primary_dev), None)
    key = chan_key if chan_key is not None else (primary_dev,)
    return key, order, len(dev_to_num)


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

    def _emit_switch(name: str, write_addr: str, state_addr: Optional[str]) -> None:
        entity_data = get_switch_config(
            name=name, address=write_addr, state_address=state_addr
        )
        entity_data["_meta"] = {
            "platform": "switch",
            "function_id": None,
            "function_type": None,
            "function_name": name,
            "usage_text": "unmapped relay channel",
            "space": "",
            "space_id": "",
        }
        entities.append(entity_data)

    # --- Step 1: Pair unmapped relay write + status-feedback by actuator channel ---
    # Group each single-device DPT-1 actuator GA by its (device, channel); pair
    # the n-th write object with the n-th status object in channel-layout order.
    # GAs that span multiple devices (central/group commands) or expose no
    # device info fall through to the address-adjacency heuristic below.
    channel_groups: Dict[tuple, Dict[str, List[tuple]]] = defaultdict(
        lambda: {"write": [], "state": []}
    )
    adjacency_fallback: List[Tuple[str, dict]] = []

    for addr, ga in group_addresses.items():
        if addr in mapped_addresses:
            continue
        cls = _classify_ga(ga, com_objects, devices)
        if cls["dpt_main"] != 1 or not cls["is_pure_actuator"]:
            continue
        role = _switch_role(cls)
        if role is None:
            continue
        key, order, num_devices = _actuator_grouping(ga, com_objects)
        if key is None or num_devices != 1:
            # No device info, or a central command across many devices.
            adjacency_fallback.append((addr, ga))
            continue
        channel_groups[key][role].append((order if order is not None else 0, addr, ga))

    for group in channel_groups.values():
        writes = sorted(group["write"])
        states = sorted(group["state"])
        for idx, (_, w_addr, w_ga) in enumerate(writes):
            s_addr = states[idx][1] if idx < len(states) else None
            _emit_switch(_clean_entity_name(w_ga["name"]), w_addr, s_addr)
            consumed.add(w_addr)
            if idx < len(states):
                consumed.add(states[idx][1])

    # --- Step 1b: Address-adjacency fallback (only when channel info is absent) ---
    adjacency_fallback.sort(key=lambda x: x[1].get("raw_address", 0))

    i = 0
    while i < len(adjacency_fallback) - 1:
        addr_a, ga_a = adjacency_fallback[i]
        addr_b, ga_b = adjacency_fallback[i + 1]
        if addr_a in consumed:
            i += 1
            continue
        raw_a = ga_a.get("raw_address", 0)
        raw_b = ga_b.get("raw_address", 0)

        if raw_b - raw_a == 1 and addr_b not in consumed:
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

            _emit_switch(_clean_entity_name(ga_a["name"]), switch_write, switch_state)
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
                "platform": "binary_sensor",
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

    # If no setpoint write GA was detected, build a read-only thermostat.
    # Never alias a write address onto the temperature sensor GA.
    return get_climate_config(
        name=name,
        temperature_state=temp_state,
        setpoint_write=setpoint_write,
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


def _platform_from_payload(ent: Dict[str, Any]) -> str:
    """Defensive fallback: infer platform from a built payload's shape.

    Builders normally record the platform in ``_meta`` at extraction time;
    this is only used if that value is missing.
    """
    knx = ent.get("knx", {})
    if "ga_sensor" in knx:
        return "binary_sensor"
    if "ga_up_down" in knx:
        return "cover"
    if "ga_temperature_current" in knx:
        return "climate"
    if "ga_switch" in knx:
        if "ga_brightness" in knx or "color" in knx or "ga_color_temp" in knx:
            return "light"
        fn_type = ent.get("_meta", {}).get("function_type", "")
        if fn_type in ("FT-1", "FT-6"):
            return "light"
        return "switch"
    return "?"


def _resolve_space_with_floor(space_id: str, locations: dict) -> Tuple[str, str]:
    """Find a space name and its containing floor name by identifier.

    Returns a ``(space_name, floor_name)`` tuple.  ``floor_name`` is built by
    joining every ancestor node name with ``" - "``.  When there are no
    ancestors the floor name is an empty string.
    """
    if not space_id:
        return "", ""

    def _search(
        spaces: dict, target: str, parent_path: list
    ) -> Optional[Tuple[str, str]]:
        for name, space in spaces.items():
            if space.get("identifier") == target:
                floor = " - ".join(parent_path) if parent_path else ""
                return name, floor
            result = _search(space.get("spaces", {}), target, parent_path + [name])
            if result:
                return result
        return None

    result = _search(locations, space_id, [])
    return result if result else ("", "")


def extract_location_hierarchy(locations: dict) -> Dict[str, Any]:
    """Walk the ETS location tree and extract floors and areas.

    ETS locations form a tree (e.g. Building → Floor → Room).  This function:

    * Records nodes as HA Areas — both leaf rooms and intermediate
      containers (which can also have KNX functions assigned).
      ``DistributionBoard`` nodes (cabinets) are intentionally skipped and
      are **not** turned into areas.
    * Builds HA Floors from ancestor paths: each unique ancestor chain that
      appears as a ``floor_name`` on any area becomes a floor.
    * Assigns a numeric ``level`` to each floor in tree-traversal order.

    Returns a dict::

        {
            "floors": [
                {"name": "Building A - Ground Floor", "level": 0},
                ...
            ],
            "areas": [
                {"name": "Living Room",   "floor_name": "Building A - Ground Floor"},
                {"name": "Building A",     "floor_name": ""},
                ...
            ],
        }
    """
    areas: List[Dict[str, Any]] = []
    floor_names: Dict[str, int] = {}  # floor_name → level

    def _walk(spaces: dict, parent_path: list) -> None:
        for name, space in spaces.items():
            children = space.get("spaces", {})
            current_path = parent_path + [name]

            # DistributionBoard nodes (cabinets) are not rooms — skip them
            # from the area list.  Still recurse into children (unlikely in
            # practice since cabinets are typically leaf nodes).
            if space.get("type") != "DistributionBoard":
                floor = " - ".join(parent_path) if parent_path else ""
                areas.append({"name": name, "floor_name": floor})

            # Recurse into children if any
            if children:
                _walk(children, current_path)

    _walk(locations, [])

    # Build floor list from the areas we found
    for area in areas:
        fn = area["floor_name"]
        if fn and fn not in floor_names:
            floor_names[fn] = len(floor_names)

    floors = [
        {"name": name, "level": level}
        for name, level in sorted(floor_names.items(), key=lambda x: x[1])
    ]

    return {"floors": floors, "areas": areas}


def collect_spaces_with_functions(entities: List[Dict[str, Any]]) -> set:
    """Return the set of ``(area_name, floor_name)`` tuples for every space
    that has at least one extracted entity (i.e. a KNX function assigned)."""
    spaces: set = set()
    for ent in entities:
        meta = ent.get("_meta", {})
        area = meta.get("space", "")
        floor = meta.get("floor", "")
        if area:
            spaces.add((area, floor))
    return spaces


def build_cabinet_promotions(
    locations: dict,
) -> Dict[str, Tuple[str, str]]:
    """Build a map of DistributionBoard-name → (parent_area_name, parent_floor_name).

    DistributionBoard nodes in ETS (cabinets, sub-panels, …) are explicitly
    typed by their ``Type`` XML attribute.  They should never become HA
    Areas — their entities belong to the containing parent area.

    Returns:
        ``{cabinet_name: (area_name, floor_name), ...}`` mapping each
        cabinet to the parent area/floor it should be promoted into.
    """
    promotions: Dict[str, Tuple[str, str]] = {}

    def _walk(spaces: dict, parent_path: list) -> None:
        for name, space in spaces.items():
            space_type = space.get("type", "")
            children = space.get("spaces", {})

            if space_type == "DistributionBoard":
                pname = parent_path[-1] if parent_path else ""
                parent_floor = (
                    " - ".join(parent_path[:-1]) if len(parent_path) > 1 else ""
                )
                promotions[name] = (pname, parent_floor)

            _walk(children, parent_path + [name])

    _walk(locations, [])
    return promotions


# ---------------------------------------------------------------------------
# Entity extraction (main entry point)
# ---------------------------------------------------------------------------


def extract_entities(
    project: dict,
    skipped: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Extract all Home Assistant entities from a parsed KNX project.

    Returns a list of entity payloads (same format as update_knx_config builders).
    Each payload has an added ``_meta`` key with KNX-specific metadata.

    Args:
        project: Parsed xknxproject dict.
        skipped: Optional list populated with functions that could not be
            converted to an entity (each ``{"name", "function_type", "reason"}``),
            so callers can report them instead of dropping them silently.
    """
    entities = []

    # 1. Extract from functions (most reliable)
    function_entities = _extract_from_functions(project, skipped=skipped)
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


def _entity_group_addresses(payload: Dict[str, Any]) -> set:
    """Collect every KNX group address referenced by an entity payload.

    Walks the ``knx`` config recursively and returns all values stored under
    ``write`` / ``state`` / ``passive`` keys. Used to de-duplicate against the
    group addresses already bound in Home Assistant.
    """
    found: set = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("write", "state", "passive") and isinstance(value, str):
                    found.add(value)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload.get("knx", {}))
    return found


async def create_floors_and_areas(
    hierarchy: Dict[str, Any],
    active_spaces: set,
    websocket,
    next_id,
    create_all_rooms: bool = False,
) -> Dict[str, str]:
    """Create HA floors and areas from the KNX project location tree.

    Always skips floors and areas that already exist in Home Assistant (matched
    by name).  Only creates areas whose ``(area_name, floor_name)`` is present
    in ``active_spaces``, unless ``create_all_rooms`` is ``True``.

    Args:
        hierarchy: Result of :func:`extract_location_hierarchy`.
        active_spaces: ``{(area_name, floor_name), ...}`` — rooms that contain
            at least one KNX function.  Ignored when *create_all_rooms*.
        websocket: Authenticated HA WebSocket connection.
        next_id: Monotonic message-id factory (``itertools.count(1).__next__``).
        create_all_rooms: If ``True``, create areas for *all* rooms in the
            project, not just those with functions.

    Returns:
        A dict mapping ``(area_name, floor_name)`` → **area_id** for every
        area that exists in HA (both newly created and pre-existing).  Keying
        by the (name, floor) tuple avoids collisions when the same room name
        appears on different floors.  Callers use this to assign entities to
        their area after entity creation.
    """
    all_floors = hierarchy.get("floors", [])
    all_areas = hierarchy.get("areas", [])

    # ---- determine which areas to create ----
    areas_to_create: List[Dict[str, Any]] = []
    if create_all_rooms:
        areas_to_create = list(all_areas)
    else:
        for area in all_areas:
            key = (area["name"], area.get("floor_name"))
            if key in active_spaces:
                areas_to_create.append(area)

    # Build the set of floor names these areas need
    needed_floor_names: set = set()
    for area in areas_to_create:
        fn = area.get("floor_name")
        if fn:
            needed_floor_names.add(fn)

    floors_to_create = [f for f in all_floors if f["name"] in needed_floor_names]

    print("\n--- HA Location Structure ---")
    print(f"  Floors needed:   {len(floors_to_create)}")
    print(f"  Areas to create: {len(areas_to_create)}")
    for f in floors_to_create:
        print(f"    Floor: {f['name']} (level={f['level']})")
    for a in areas_to_create:
        parent = f" [{a['floor_name']}]" if a.get("floor_name") else ""
        print(f"    Area:  {a['name']}{parent}")

    # ---- fetch existing floors / areas from HA ----
    existing_floors_resp = await list_floors(websocket, next_id())
    existing_floors = existing_floors_resp.get("result") or []
    existing_floor_by_name: Dict[str, str] = {
        f["name"]: f["floor_id"] for f in existing_floors
    }

    existing_areas_resp = await list_areas(websocket, next_id())
    existing_areas = existing_areas_resp.get("result") or []
    # Map existing HA areas to their floor name so we can key by
    # (name, floor_name) and avoid colliding same-named rooms on
    # different floors.
    existing_floor_name_by_id: Dict[str, str] = {
        f["floor_id"]: f["name"] for f in existing_floors
    }
    existing_area_by_name: Dict[tuple, str] = {
        (a["name"], existing_floor_name_by_id.get(a.get("floor_id"), "")): a["area_id"]
        for a in existing_areas
    }

    # ---- create floors ----
    ha_floor_ids: Dict[str, str] = dict(existing_floor_by_name)  # name → floor_id
    created_floors = 0
    skipped_floors = 0

    for floor in floors_to_create:
        name = floor["name"]
        if name in existing_floor_by_name:
            print(f"  SKIP floor (exists): {name}")
            skipped_floors += 1
            continue
        result = await create_floor(
            websocket, next_id(), name, level=floor.get("level")
        )
        if result.get("success") and result.get("result", {}).get("floor_id"):
            fid = result["result"]["floor_id"]
            ha_floor_ids[name] = fid
            created_floors += 1
            print(f"  OK floor: {name} → {fid}")
        else:
            print(f"  FAIL floor: {name}: {result}")

    # ---- create areas ----
    # Keyed by (name, floor_name) so same-named rooms on different floors
    # don't collide.
    ha_area_ids: Dict[tuple, str] = dict(existing_area_by_name)
    created_areas = 0
    skipped_areas = 0

    for area in areas_to_create:
        name = area["name"]
        floor_name = area.get("floor_name") or ""
        if (name, floor_name) in existing_area_by_name:
            print(f"  SKIP area (exists): {name}")
            skipped_areas += 1
            continue

        if floor_name:
            floor_id = ha_floor_ids.get(floor_name)
        elif area["name"] in ha_floor_ids:
            # Self-referencing: this area and a floor share the same
            # name (e.g. "Outdoor Area" is both a container and a
            # location in its own right).  Nest the area under the
            # matching floor.
            floor_id = ha_floor_ids[area["name"]]
        else:
            floor_id = None
        result = await create_area(websocket, next_id(), name, floor_id=floor_id)
        if result.get("success") and result.get("result", {}).get("area_id"):
            aid = result["result"]["area_id"]
            ha_area_ids[(name, floor_name)] = aid
            created_areas += 1
            print(
                f"  OK area: {name} → {aid}"
                + (f" (floor={floor_id})" if floor_id else "")
            )
        else:
            print(f"  FAIL area: {name}: {result}")

    print(
        f"  Floors: {created_floors} created, {skipped_floors} skipped; "
        f"Areas: {created_areas} created, {skipped_areas} skipped"
    )

    return ha_area_ids


async def create_entities_batch(
    entities: List[Dict[str, Any]],
    url: str,
    token: str,
    dry_run: bool = True,
    skip_existing: bool = True,
    filter_platform: Optional[str] = None,
    filter_location: Optional[str] = None,
    area_map: Optional[Dict[tuple, str]] = None,
    websocket: Any = None,
    next_id: Any = None,
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
        area_map: Optional ``{(area_name, floor_name): area_id}`` mapping.
            When provided, each successfully created entity is assigned to its
            area via ``config/entity_registry/update``.
        websocket: Optional pre-existing authenticated WebSocket connection.
            When supplied the function reuses it instead of opening a new one
            (the caller is responsible for closing).
        next_id: Optional monotonic message-id factory
            (``itertools.count(1).__next__``).  When a shared *websocket* is
            provided this MUST be supplied to avoid id-reuse errors.

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

    # Reusing a shared websocket without a shared id source would restart
    # message ids at 1 and collide with ids already used on that socket.
    if websocket is not None and next_id is None:
        raise ValueError(
            "next_id must be supplied when reusing an existing websocket connection"
        )

    # Connect and authenticate (reuse existing websocket if provided)
    own_ws = websocket is None
    if own_ws:
        print(f"Connecting to {url}...")
        websocket = await connect_and_authenticate(url, token)

    summary = {"total": len(filtered), "created": 0, "skipped": 0, "failed": 0}

    # Single monotonic message-id source for every request in this session,
    # so ids can never collide regardless of how many calls are made.
    if next_id is None:
        next_id = itertools.count(1).__next__

    try:
        # Get the group addresses already bound to existing entities. Matching
        # on group address (not display name) is the reliable dedup signal:
        # the KNX UI assigns entity unique_ids server-side at creation, so they
        # cannot be predicted here, and names are locale/slug sensitive.
        existing_gas = set()
        if skip_existing:
            grp_result = await get_entities_by_group(websocket, next_id())
            existing_gas = set((grp_result.get("result") or {}).keys())

        for i, ent in enumerate(filtered):
            meta = ent.get("_meta", {})
            platform = meta.get("platform", "?")
            entity_name = ent.get("entity", {}).get("name", "unknown")
            fn_name = meta.get("function_name", "")

            # Strip _meta before sending
            payload = {k: v for k, v in ent.items() if k != "_meta"}

            # Skip if any of this entity's group addresses is already bound.
            if skip_existing:
                already = _entity_group_addresses(payload) & existing_gas
                if already:
                    print(
                        f"  [{i + 1}/{len(filtered)}] SKIP (existing GA "
                        f"{', '.join(sorted(already))}): {entity_name}"
                    )
                    summary["skipped"] += 1
                    continue

            print(f"  [{i + 1}/{len(filtered)}] Creating {platform}: {entity_name}")

            try:
                # Validate
                val_result = await validate_entity(
                    websocket, next_id(), platform, payload
                )

                if not (
                    val_result.get("success")
                    and val_result.get("result", {}).get("success")
                ):
                    print(f"    Validation failed: {json.dumps(val_result, indent=4)}")
                    summary["failed"] += 1
                    continue

                # Create
                create_result = await create_entity(
                    websocket, next_id(), platform, payload
                )

                if create_result.get("success"):
                    entity_id = create_result.get("result", {}).get("entity_id")
                    print(f"    OK" + (f" → {entity_id}" if entity_id else ""))
                    summary["created"] += 1

                    # Assign entity to its area if we have a mapping.
                    # Look up by (space, floor) so same-named rooms on
                    # different floors resolve to the correct area.  An
                    # entity with empty space/floor resolves to None.
                    if area_map and entity_id:
                        space = meta.get("space", "")
                        floor = meta.get("floor", "")
                        area_id = area_map.get((space, floor))
                        if area_id:
                            try:
                                await update_entity_area(
                                    websocket, next_id(), entity_id, area_id
                                )
                                print(f"    → assigned to area '{space}' ({area_id})")
                            except Exception as exc:
                                print(f"    → failed to assign area: {exc}")
                else:
                    print(f"    Failed: {json.dumps(create_result, indent=4)}")
                    summary["failed"] += 1

            except Exception as exc:
                print(f"    Error: {exc}")
                summary["failed"] += 1

    finally:
        if own_ws:
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
    parser.add_argument(
        "--skip-rooms",
        action="store_true",
        help="Do not create HA areas/floors from project locations. "
        "Entities will not be assigned to areas either.",
    )
    parser.add_argument(
        "--create-all-rooms",
        action="store_true",
        help="Create HA areas/floors for ALL rooms found in the project, "
        "even those without any mapped KNX functions. "
        "By default only rooms that contain functions are created.",
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


async def main():
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
    skipped: List[Dict[str, str]] = []
    entities = extract_entities(project, skipped=skipped)

    # Report functions that could not be converted, so they don't vanish.
    if skipped:
        print(f"\nSkipped {len(skipped)} function(s) (no entity created):")
        for s in skipped:
            ft = s.get("function_type") or "?"
            print(f"  - {s['name']!r} [{ft}]: {s['reason']}")

    if not entities:
        print("\nNo entities could be extracted from the project.")
        return

    # Platform is set by the builders at extraction time (see
    # _extract_from_functions / _extract_unmapped). As a defensive safety net
    # only, backfill it from the payload shape if it is somehow missing.
    for ent in entities:
        meta = ent.setdefault("_meta", {})
        if not meta.get("platform"):
            meta["platform"] = _platform_from_payload(ent)

    # Promote cabinet functions to their parent area.  In ETS, distribution
    # cabinets (DB-01, DB-02, …) sit inside areas (North Zone,
    # South Zone, …).  If both cabinet and parent have functions, the
    # cabinet is NOT an area — its entities belong to the parent.
    locations = project.get("locations", {})
    if locations:
        promotions = build_cabinet_promotions(locations)
        if promotions:
            for ent in entities:
                meta = ent.get("_meta", {})
                space = meta.get("space", "")
                if space in promotions:
                    parent_area, parent_floor = promotions[space]
                    meta["space"] = parent_area
                    meta["floor"] = parent_floor
            print(f"\nPromoted {len(promotions)} cabinet(s) to parent areas:")
            for cab, (area, floor) in sorted(promotions.items()):
                fn = f" [{floor}]" if floor else ""
                print(f"  {cab} → {area}{fn}")

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
        area_map: Dict[tuple, str] = {}
        create_rooms = not args.skip_rooms

        if create_rooms and locations:
            hierarchy = extract_location_hierarchy(locations)
            active_spaces = collect_spaces_with_functions(entities)

            # Connect once and reuse for both room + entity creation.
            print(f"\nConnecting to {args.url}...")
            websocket = await connect_and_authenticate(args.url, token)
            next_id = itertools.count(1).__next__
            try:
                area_map = await create_floors_and_areas(
                    hierarchy=hierarchy,
                    active_spaces=active_spaces,
                    websocket=websocket,
                    next_id=next_id,
                    create_all_rooms=args.create_all_rooms,
                )

                summary = await create_entities_batch(
                    entities=entities,
                    url=args.url,
                    token=token,
                    dry_run=False,
                    skip_existing=args.skip_existing,
                    filter_platform=args.filter_type,
                    filter_location=args.filter_location,
                    area_map=area_map,
                    websocket=websocket,
                    next_id=next_id,
                )
            finally:
                await websocket.close()
                print("Connection closed.")
        else:
            if args.skip_rooms and locations:
                print("\nRoom/area creation skipped (--skip-rooms).")
            summary = await create_entities_batch(
                entities=entities,
                url=args.url,
                token=token,
                dry_run=False,
                skip_existing=args.skip_existing,
                filter_platform=args.filter_type,
                filter_location=args.filter_location,
                area_map={},
            )

        print(
            f"\nDone: {summary['created']} created, "
            f"{summary['skipped']} skipped, "
            f"{summary['failed']} failed"
        )


if __name__ == "__main__":
    asyncio.run(main())
