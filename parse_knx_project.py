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
    check_knx_integration,
    is_unknown_command,
    KNXIntegrationNotInstalledError,
    ws_url_to_http,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Map KNX function types to HA entity platforms
FUNCTION_TYPE_MAP: Dict[str, Dict[str, str]] = {
    "FT-1": {"platform": "light", "usage": "switchable light"},
    "FT-6": {"platform": "light", "usage": "dimmable light"},
    "FT-7": {"platform": "cover", "usage": "sun protection"},
    "FT-8": {"platform": "climate", "usage": "heating"},
    "FT-9": {"platform": "climate", "usage": "heating (continuous variable)"},
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


def _status_name_token(name: str) -> str:
    """Normalise a com-object identifier for command<->status matching.

    The status object's product identifier is the command's with a status
    marker added, e.g. ``"A-G2 Schalten"`` (command) vs ``"A-G2 Status
    Schalten"`` (status). Removing the marker and collapsing whitespace makes
    the two equal, so a command can be paired to *its own* status even when a
    multiplexing gateway puts many groups on one ETS channel.
    """
    stripped = re.sub(r"(?i)\b(status|fb|feedback|r[uü]ckmeldung)\b", " ", name or "")
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _extract_unmapped(
    project: dict,
    mapped_addresses: set,
) -> List[Dict[str, Any]]:
    """Deterministically extract entities from GAs not covered by any function.

    OPT-IN fallback (enabled by ``--include-unmapped``). It uses the same
    actuator-linkage model as the function builders — never address adjacency
    or user GA labels — so it only emits entities that follow from the bus
    topology and the manufacturer's product-database object identifiers.

    Devices are first classified by the objects they own across unmapped GAs:

    - *sender* — owns a Write+Transmit object (push button, scene logic,
      visualisation gateway): never a load actuator;
    - *cover* — owns a DPT-1.008 command (a blind drive): its DPT-1 GAs are
      sun-protection / drive-status, not switches;
    - *actuator* — owns a DPT-1.001 command (Write, not Transmit): a load
      driver (relay, DALI gateway).

    Switches: a GA with exactly one DPT-1.001 command object from a single
    non-sender, non-cover device is a single load. (A GA carrying several such
    objects is a central/group command — "all kitchen lights" — and is left
    out.) Its status is the same device's Transmit-not-Write DPT-1.001/1.011
    object whose product identifier matches by :func:`_status_name_token`;
    failing that, the lone status object on the same channel.

    Binary sensors: a pure-source DPT-1 GA (a Transmit-not-Write object and no
    write object at all) whose source device is not an actuator or cover —
    i.e. a field sensor (motion/contact), not an actuator's own status feedback.
    """
    group_addresses = project.get("group_addresses", {})
    com_objects = project.get("communication_objects", {})
    entities: List[Dict[str, Any]] = []

    def _emit(entity_data: dict, platform: str, name: str, usage: str) -> None:
        entity_data["_meta"] = {
            "platform": platform,
            "function_id": None,
            "function_type": None,
            "function_name": name,
            "usage_text": usage,
            "space": "",
            "space_id": "",
        }
        entities.append(entity_data)

    # --- Classify devices by the objects they own across unmapped GAs. ---
    sender_devices: set = set()
    cover_devices: set = set()
    actuator_devices: set = set()
    for addr, ga in group_addresses.items():
        if addr in mapped_addresses:
            continue
        dpt = ga.get("dpt") or {}
        main, sub = dpt.get("main"), dpt.get("sub")
        for co_id in ga.get("communication_object_ids", []):
            co = com_objects.get(co_id, {})
            dev = co.get("device_address", "")
            if not dev:
                continue
            flags = co.get("flags", {})
            write = flags.get("write", False)
            transmit = flags.get("transmit", False)
            if write and transmit:
                sender_devices.add(dev)
            elif write and not transmit and main == 1 and sub == 8:
                cover_devices.add(dev)
            elif write and not transmit and main == 1 and sub == 1:
                actuator_devices.add(dev)

    # --- Build a per-device index of status objects for command<->status pairing. ---
    # device -> list of (name_token, channel, addr)
    status_index: Dict[str, list] = defaultdict(list)
    for addr, ga in group_addresses.items():
        if addr in mapped_addresses:
            continue
        dpt = ga.get("dpt") or {}
        if dpt.get("main") != 1 or dpt.get("sub") not in (1, 11):
            continue
        for co_id in ga.get("communication_object_ids", []):
            co = com_objects.get(co_id, {})
            dev = co.get("device_address", "")
            if not dev or dev in sender_devices:
                continue
            flags = co.get("flags", {})
            if flags.get("transmit") and not flags.get("write"):
                status_index[dev].append(
                    (_status_name_token(co.get("name", "")), co.get("channel"), addr)
                )

    # --- Switches: single-load DPT-1.001 commands, status paired by identifier. ---
    consumed: set = set()
    for addr, ga in group_addresses.items():
        if addr in mapped_addresses:
            continue
        dpt = ga.get("dpt") or {}
        if dpt.get("main") != 1 or dpt.get("sub") != 1:
            continue

        command_objs = []
        for co_id in ga.get("communication_object_ids", []):
            co = com_objects.get(co_id, {})
            dev = co.get("device_address", "")
            if not dev or dev in sender_devices or dev in cover_devices:
                continue
            flags = co.get("flags", {})
            if flags.get("write") and not flags.get("transmit"):
                command_objs.append(co)

        # Exactly one command object => a single load (not a group command).
        if len(command_objs) != 1:
            continue
        co = command_objs[0]
        dev = co.get("device_address", "")
        token = _status_name_token(co.get("name", ""))
        channel = co.get("channel")

        candidates = status_index.get(dev, [])
        token_match = [s_addr for tok, _ch, s_addr in candidates if tok and tok == token]
        channel_match = [s_addr for _tok, ch, s_addr in candidates if ch == channel]
        if token_match:
            state_addr = token_match[0]
        elif len(channel_match) == 1:
            state_addr = channel_match[0]
        else:
            state_addr = None

        name = ga.get("name", "")
        entity_data = get_switch_config(
            name=_clean_entity_name(name) or f"Switch {addr}",
            address=addr,
            state_address=state_addr,
        )
        _emit(entity_data, "switch", name, "unmapped relay channel")
        consumed.add(addr)
        if state_addr:
            consumed.add(state_addr)

    # --- Binary sensors: pure-source DPT-1 GAs from non-actuator field devices. ---
    for addr, ga in group_addresses.items():
        if addr in mapped_addresses or addr in consumed:
            continue
        if (ga.get("dpt") or {}).get("main") != 1:
            continue
        source_devices = set()
        has_write = False
        for co_id in ga.get("communication_object_ids", []):
            co = com_objects.get(co_id, {})
            flags = co.get("flags", {})
            if flags.get("write"):
                has_write = True
            if flags.get("transmit") and not flags.get("write"):
                source_devices.add(co.get("device_address", ""))
        if has_write or not source_devices:
            continue
        # A status fed by an actuator/cover is that load's feedback, not a sensor.
        if source_devices & (actuator_devices | cover_devices):
            continue
        name = _clean_entity_name(ga.get("name", "")) or f"Binary {addr}"
        entity_data = get_binary_sensor_config(name=name, state_address=addr)
        _emit(entity_data, "binary_sensor", ga.get("name", ""), "binary sensor input")

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
        return _build_cover(entity_name, gas, com_objects, devices)

    return None


def _co_direction(co: dict) -> Optional[str]:
    """Classify a single com object's direction from its flags.

    Returns ``"command"`` for a pure command sink (Write and not Transmit),
    ``"feedback"`` for a pure feedback source (Transmit and not Write), or
    ``None`` for a directionless object — one that is both Write and Transmit
    (a visualisation/logic gateway, or a push-button send object) or neither.
    """
    flags = co.get("flags", {})
    write = flags.get("write", False)
    transmit = flags.get("transmit", False)
    if write and not transmit:
        return "command"
    if transmit and not write:
        return "feedback"
    return None


def _select_actuator(gas: List[Tuple[str, dict]], com_objects: dict) -> str:
    """Deterministically identify the load actuator from com-object linkage.

    The actuator is the device that *receives* the function's commands and
    *sends* its feedback, so across the function's group addresses it owns
    ``command`` objects (Write, not Transmit) and ``feedback`` objects
    (Transmit, not Write). This distinguishes it from:

    - push buttons / sensors, which *transmit* commands (Write+Transmit) and
      only sink status into their indicator LEDs;
    - visualisation / logic gateways, whose objects are Read+Write+Transmit on
      every GA and therefore directionless (:func:`_co_direction` → ``None``).

    Devices are ranked by ``(#feedback objects, #command objects)`` — feedback
    first, deliberately: a push button's indicator LEDs *receive* status, so
    they look like command sinks and can out-count the real actuator's single
    command input (see the indicator-LED two-GA case). Only the load
    actuator is a *source* of the load's feedback, so the feedback count breaks
    that tie correctly.

    A device is disqualified outright if it owns any *sender* object
    (Write+Transmit) on the function — that is the push-button / wall-switch /
    visualisation signature (they transmit commands onto the bus). A real load
    actuator's inputs are write-only and its outputs transmit-only; it never
    transmits a command. Without this guard, a function whose real actuator is
    not linked (e.g. a DALI ballast missing from the export) would fall back to
    a push button whose only command-looking object is an indicator-LED status
    sink, producing a switch address pointed at the feedback GA. A device must
    also own at least one command object to qualify. Returns ``""`` when no
    device qualifies — the caller then skips and reports the function.
    """
    commands: Dict[str, int] = {}
    feedbacks: Dict[str, int] = {}
    senders: Dict[str, int] = {}
    for _addr, ga in gas:
        for co_id in ga.get("communication_object_ids", []):
            co = com_objects.get(co_id, {})
            dev = co.get("device_address", "")
            if not dev:
                continue
            flags = co.get("flags", {})
            write = flags.get("write", False)
            transmit = flags.get("transmit", False)
            if write and transmit:
                senders[dev] = senders.get(dev, 0) + 1
            elif write:
                commands[dev] = commands.get(dev, 0) + 1
            elif transmit:
                feedbacks[dev] = feedbacks.get(dev, 0) + 1
    qualifying = [
        dev
        for dev, n in commands.items()
        if n > 0 and senders.get(dev, 0) == 0
    ]
    if not qualifying:
        return ""
    return max(qualifying, key=lambda d: (feedbacks.get(d, 0), commands[d]))


def _actuator_ga_dirs(
    ga: dict, com_objects: dict, actuator: str
) -> Tuple[bool, bool]:
    """``(has_command, has_feedback)`` for the actuator's objects on this GA.

    Both can be true at once: a multi-channel actuator may cross-link a foreign
    channel's command onto another channel's feedback GA, and a value gateway
    may echo its set-value back on the same GA. The pairing in
    :func:`_pick_command_feedback` resolves which slot each GA fills, so this
    reports the raw directions rather than forcing a single role. Other devices
    on the GA (senders, visualisation gateways) are ignored.
    """
    has_command = has_feedback = False
    if actuator:
        for co_id in ga.get("communication_object_ids", []):
            co = com_objects.get(co_id, {})
            if co.get("device_address", "") != actuator:
                continue
            direction = _co_direction(co)
            if direction == "command":
                has_command = True
            elif direction == "feedback":
                has_feedback = True
    return has_command, has_feedback


def _pick_command_feedback(
    bucket: List[Tuple[str, bool, bool]],
) -> Tuple[Optional[str], Optional[str]]:
    """Pick ``(command, feedback)`` addresses from one DPT role bucket.

    ``bucket`` items are ``(address, has_command, has_feedback)`` in GA order.
    A GA that carries only one direction (pure command / pure feedback) is
    preferred for its slot, so a cross-linked or echoed GA that carries both
    never steals a slot from the unambiguous GA. The feedback address must
    differ from the chosen command address, so one GA can never fill both.
    """
    pure_command = [a for a, c, f in bucket if c and not f]
    any_command = [a for a, c, f in bucket if c]
    command = (pure_command or any_command or [None])[0]

    pure_feedback = [a for a, c, f in bucket if f and not c and a != command]
    any_feedback = [a for a, c, f in bucket if f and a != command]
    feedback = (pure_feedback or any_feedback or [None])[0]

    return command, feedback


def _build_light(
    name: str,
    gas: List[Tuple[str, dict]],
    com_objects: dict,
    function_type: str,
    devices: dict = None,
) -> Optional[Dict[str, Any]]:
    """Build a light entity from actuator-linkage roles per DPT.

    The load actuator is identified from com-object linkage
    (:func:`_select_actuator`); GAs are bucketed by DPT (switch = DPT 1,
    brightness = DPT 5 absolute value) and each bucket's command/feedback pair
    is resolved by :func:`_pick_command_feedback` from the actuator's own object
    directions. DPT 3 (relative dim) and DPT 7.600 (colour temperature) are
    intentionally ignored — HA needs only switch and brightness here.
    """
    actuator = _select_actuator(gas, com_objects)

    switch_bucket: List[Tuple[str, bool, bool]] = []
    brightness_bucket: List[Tuple[str, bool, bool]] = []

    for addr, ga in gas:
        dpt_main = (ga.get("dpt") or {}).get("main")
        if dpt_main not in (1, 5):
            continue
        has_command, has_feedback = _actuator_ga_dirs(ga, com_objects, actuator)
        if not (has_command or has_feedback):
            continue
        entry = (addr, has_command, has_feedback)
        if dpt_main == 1:
            switch_bucket.append(entry)
        else:  # DPT 5 — absolute brightness value
            brightness_bucket.append(entry)

    switch_write, switch_state = _pick_command_feedback(switch_bucket)
    brightness_write, brightness_state = _pick_command_feedback(brightness_bucket)

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
    """Build a switch entity from the DPT-1 GAs of a function.

    Command and feedback are resolved from the load actuator's own object
    directions (:func:`_select_actuator` + :func:`_actuator_ga_dirs` +
    :func:`_pick_command_feedback`), so senders, visualisation gateways, and
    foreign cross-linked channels sharing a GA do not confuse the assignment.
    """
    actuator = _select_actuator(gas, com_objects)

    bucket: List[Tuple[str, bool, bool]] = []
    for addr, ga in gas:
        if (ga.get("dpt") or {}).get("main") != 1:
            continue
        has_command, has_feedback = _actuator_ga_dirs(ga, com_objects, actuator)
        if has_command or has_feedback:
            bucket.append((addr, has_command, has_feedback))

    switch_write, switch_state = _pick_command_feedback(bucket)

    if switch_write is None:
        return None

    return get_switch_config(
        name=name,
        address=switch_write,
        state_address=switch_state,
    )


def _actuator_dpt9_objects(ga: dict, com_objects: dict, actuator: str):
    """Yield ``(direction, sem)`` for the actuator's DPT-9.001 objects on a GA.

    ``direction`` is from :func:`_co_direction`; ``sem`` is the lower-cased
    concatenation of the object's structured identifier, function text and
    display text. Only DPT 9.001 (temperature) objects are yielded, so
    humidity (9.007) and CO2 (9.008) on the same function are never considered.
    """
    for co_id in ga.get("communication_object_ids", []):
        co = com_objects.get(co_id, {})
        if co.get("device_address", "") != actuator:
            continue
        dpts = co.get("dpts") or []
        if not any(d.get("main") == 9 and d.get("sub") == 1 for d in dpts):
            continue
        sem = " ".join(
            str(co.get(k, "") or "") for k in ("name", "function_text", "text")
        ).lower()
        yield _co_direction(co), sem


# Keywords (in the controller's own object identifier / function text) that
# pick the current-temperature feedback object, best first. The controller
# exposes several DPT-9.001 feedback temperatures (effective/control, probe,
# raw source); the effective/control temperature is what HA should display.
_TEMP_CURRENT_KEYWORDS = ("effective", "actual", "current", "room", "operative")


def _build_climate(
    name: str,
    gas: List[Tuple[str, dict]],
    com_objects: dict,
    group_addresses: dict,
    devices: dict = None,
) -> Optional[Dict[str, Any]]:
    """Build a climate entity from the thermostat controller's linkage.

    The controller (room thermostat / heating channel) is identified from
    com-object linkage (:func:`_select_actuator`). Temperature and setpoint are
    both DPT 9.001 with identical flags, so they cannot be told apart by
    direction alone — the controller's structured object identifier
    (``name`` / ``function_text``, e.g. ``oTh[0].tempSetpoint`` vs
    ``oTh[0].tempEffective``) is the deterministic discriminator. Only the
    controller's own objects are inspected, so the visualisation gateway and
    the wall RTC sharing the GAs are ignored.

    Mapping (all DPT 9.001, restricted to the controller):
      - object identifier contains "setpoint", command  → setpoint write
      - object identifier contains "setpoint", feedback  → setpoint state
      - feedback, not setpoint, best temperature keyword → current temperature

    DPT 5 control-variable outputs (to the valve), DPT 9.007/9.008
    humidity/CO2, and the external-sensor *input* objects are all ignored.
    """
    actuator = _select_actuator(gas, com_objects)
    if not actuator:
        return None

    temp_current = None
    temp_current_rank = len(_TEMP_CURRENT_KEYWORDS) + 1
    setpoint_write = None
    setpoint_state = None

    for addr, ga in gas:
        for direction, sem in _actuator_dpt9_objects(ga, com_objects, actuator):
            if "setpoint" in sem:
                if direction == "command" and setpoint_write is None:
                    setpoint_write = addr
                elif direction == "feedback" and setpoint_state is None:
                    setpoint_state = addr
            elif direction == "feedback":
                # Current-temperature candidate; rank by keyword so the
                # effective/control temperature beats the raw probe value.
                rank = next(
                    (i for i, kw in enumerate(_TEMP_CURRENT_KEYWORDS) if kw in sem),
                    len(_TEMP_CURRENT_KEYWORDS),
                )
                if rank < temp_current_rank:
                    temp_current = addr
                    temp_current_rank = rank

    if temp_current is None:
        return None

    return get_climate_config(
        name=name,
        temperature_state=temp_current,
        setpoint_write=setpoint_write,
        setpoint_state=setpoint_state,
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


def _build_cover(
    name: str,
    gas: List[Tuple[str, dict]],
    com_objects: dict,
    devices: dict = None,
) -> Optional[Dict[str, Any]]:
    """Build a cover entity from actuator-linkage roles per DPT sub-type.

    The blind actuator is identified from com-object linkage
    (:func:`_select_actuator`); GAs are bucketed by DPT sub-type and each
    bucket's command/feedback pair is resolved by
    :func:`_pick_command_feedback` from the actuator's own object directions:

    - DPT 1.008, command → up/down (long move)
    - DPT 1.007, command → step/stop
    - DPT 5.001 → position; command → set, feedback → state

    Other DPT-1 sub-types on the function (1.001 sun-protection enable,
    drive-status feedback, ...) are deliberately ignored — mapping them onto
    up/down is exactly the bug this replaces. A function whose actuator exposes
    no DPT-1.008 command has no usable direction GA and is skipped (the caller
    reports it).
    """
    actuator = _select_actuator(gas, com_objects)

    up_down_bucket: List[Tuple[str, bool, bool]] = []
    stop_bucket: List[Tuple[str, bool, bool]] = []
    position_bucket: List[Tuple[str, bool, bool]] = []

    for addr, ga in gas:
        dpt = ga.get("dpt") or {}
        dpt_main = dpt.get("main")
        dpt_sub = dpt.get("sub")
        has_command, has_feedback = _actuator_ga_dirs(ga, com_objects, actuator)
        if not (has_command or has_feedback):
            continue
        entry = (addr, has_command, has_feedback)
        if dpt_main == 1 and dpt_sub == 8:
            up_down_bucket.append(entry)
        elif dpt_main == 1 and dpt_sub == 7:
            stop_bucket.append(entry)
        elif dpt_main == 5:
            position_bucket.append(entry)

    # Up/down and stop are command-only roles for HA; ignore any feedback.
    up_down_write, _ = _pick_command_feedback(up_down_bucket)
    stop_write, _ = _pick_command_feedback(stop_bucket)
    position_set_write, position_state = _pick_command_feedback(position_bucket)

    if up_down_write is None:
        return None

    return get_cover_config(
        name=name,
        up_down_write=up_down_write,
        stop_write=stop_write,
        position_set_write=position_set_write,
        position_state=position_state,
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
        if fn_type in FUNCTION_TYPE_MAP:
            return FUNCTION_TYPE_MAP[fn_type]["platform"]
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
    include_unmapped: bool = False,
) -> List[Dict[str, Any]]:
    """Extract all Home Assistant entities from a parsed KNX project.

    Returns a list of entity payloads (same format as update_knx_config builders).
    Each payload has an added ``_meta`` key with KNX-specific metadata.

    Args:
        project: Parsed xknxproject dict.
        skipped: Optional list populated with functions that could not be
            converted to an entity (each ``{"name", "function_type", "reason"}``),
            so callers can report them instead of dropping them silently.
        include_unmapped: When True, also run the opt-in deterministic fallback
            (:func:`_extract_unmapped`) over GAs that belong to no function.
            Default False — only function-derived entities are returned.
    """
    entities = []

    # 1. Extract from functions (most reliable)
    function_entities = _extract_from_functions(project, skipped=skipped)
    entities.extend(function_entities)

    if not include_unmapped:
        return entities

    # Track which GAs belong to a function. This must include the GAs of
    # functions that were *skipped* (not just those successfully built):
    # ETS grouped those GAs into a function deliberately, so if we could not
    # build it we must not let the unmapped fallback re-interpret its
    # individual GAs as loose switches — that yields garbage such as a switch
    # whose address is actually a status-feedback GA.
    mapped = set()
    for fn in project.get("functions", {}).values():
        for addr in fn.get("group_addresses", {}):
            mapped.add(addr)

    # 2. Extract unmapped GAs (opt-in deterministic fallback)
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
        # Pre-flight: verify the KNX integration is installed.  When using a
        # shared websocket the caller (main) already checked, but when we
        # opened our own connection we must check here.
        if own_ws:
            await check_knx_integration(
                websocket, next_id(), ha_url=ws_url_to_http(url)
            )

        # Get the group addresses already bound to existing entities. Matching
        # on group address (not display name) is the reliable dedup signal:
        # the KNX UI assigns entity unique_ids server-side at creation, so they
        # cannot be predicted here, and names are locale/slug sensitive.
        existing_gas = set()
        if skip_existing:
            grp_result = await get_entities_by_group(websocket, next_id())
            # Covers the shared-websocket path, where main() ran the
            # integration pre-flight but this function did not (own_ws is
            # False, so the check above was skipped).
            if is_unknown_command(grp_result):
                raise KNXIntegrationNotInstalledError(
                    "HA returned 'unknown_command' for knx/get_entities_by_group.",
                    ha_url=ws_url_to_http(url),
                )
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
    parser.add_argument(
        "--include-unmapped",
        action="store_true",
        help="Also extract entities from group addresses that belong to no ETS "
        "function, using deterministic actuator linkage (relay channels -> "
        "switches, pure-source DPT-1 inputs -> binary_sensors). Off by "
        "default: only entities defined by ETS functions are produced.",
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
    entities = extract_entities(
        project, skipped=skipped, include_unmapped=args.include_unmapped
    )

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

        try:
            if create_rooms and locations:
                hierarchy = extract_location_hierarchy(locations)
                active_spaces = collect_spaces_with_functions(entities)

                # Connect once and reuse for both room + entity creation.
                print(f"\nConnecting to {args.url}...")
                websocket = await connect_and_authenticate(args.url, token)
                next_id = itertools.count(1).__next__
                try:
                    # Pre-flight: verify the KNX integration is installed
                    # before doing any work.  Without it every knx/* command
                    # silently returns "unknown_command" and the run produces
                    # hundreds of identical failures that obscure the root
                    # cause.
                    await check_knx_integration(
                        websocket, next_id(), ha_url=ws_url_to_http(args.url)
                    )

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
        except KNXIntegrationNotInstalledError as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
