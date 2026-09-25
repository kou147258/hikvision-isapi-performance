"""Tests for the v0.6.19 audit cleanup.

v0.6.19 changes under test:

- ``_or_none`` helper normalises empty strings (``""``) to ``None``
  so V5 IPC fields like ``<encoderVersion></encoderVersion>`` show
  "unknown" instead of blank cells.
- ``device_status`` derives from coordinator data ("在线" /
  "离线") instead of reading the missing V4 NVR XML field.
- New sensors: ``mtu``, ``time_mode``, ``network_mtu`` already
  existed as raw data, now exposed.
- Removed: ``encoder_release_date`` (V4-only, low value vs
  ``encoder_version``).
- New binary sensor: ``device_online`` (CONNECTIVITY class).
- New buttons: four PTZ direction buttons registered only when
  ``capabilities["ptz"]`` is True.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_SENSOR_SRC = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance\sensor.py"
)
_BUTTON_SRC = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance\button.py"
)
_BINARY_SRC = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance\binary_sensor.py"
)
_COORD_SRC = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance\coordinator.py"
)


def _sensor_keys() -> list[str]:
    """Read sensor.py and pull out the ``key="..."`` from each entry."""
    import re

    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    return re.findall(r'key="([a-zA-Z0-9_]+)"', src)


def _extract_function_body(source: str, def_line: str) -> str | None:
    """Return the body of the first function whose definition starts with
    ``def_line``, or ``None`` if not found.

    The "body" is everything from the def line up to (but not
    including) the next top-level (``column 0``) ``def`` / ``class`` /
    ``@``. Stops at the first such boundary or end-of-file. The
    docstring + indentation are included — the caller can grep them.
    """
    idx = source.find(def_line)
    if idx < 0:
        return None
    rest = source[idx + len(def_line):]
    # Find the next top-level definition: a line starting with
    # ``def`` / ``class`` / ``@`` at column 0 (no leading whitespace).
    boundary = None
    for marker in ("\ndef ", "\nclass ", "\n@"):
        pos = rest.find(marker, 1)
        if pos > 0 and (boundary is None or pos < boundary):
            boundary = pos
    if boundary is None:
        return rest
    return rest[:boundary]


# ---- _or_none helper ----


def test_or_none_helper_exists():
    """sensor.py must define ``_or_none``."""
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    assert "def _or_none(" in src, (
        "sensor.py must define the ``_or_none`` helper (v0.6.19)."
    )


def test_or_none_normalises_empty_strings():
    """_or_none(""), _or_none("   "), _or_none(None) all return None."""
    import re

    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    # Pull the body of _or_none up to the next def or class at column 0.
    body = _extract_function_body(src, "def _or_none(")
    assert body is not None, "_or_none function not found."
    assert "is None" in body, "_or_none must short-circuit on None."
    assert ".strip()" in body, "_or_none must strip whitespace."
    # The final return should be the stripped string or None.
    assert re.search(r"stripped\s+or\s+None", body), (
        "_or_none must return ``stripped or None`` for strings."
    )
    # Real values pass through (no None coercion for non-empty).
    assert re.search(r"return value\b", body), (
        "_or_none must pass non-string values through unchanged."
    )


# ---- device_status ----


def test_device_status_derived_from_data():
    """device_status sensor no longer reads XML — it derives from data."""
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    body = _extract_function_body(src, "def _device_status_text(")
    assert body is not None, "_device_status_text function not found."
    assert "device_info" in body, (
        "_device_status_text must inspect coordinator.device_info."
    )
    # Empty device_info → 离线; populated → 在线
    assert '"离线"' in body or "'离线'" in body, (
        "_device_status_text must return 离线 for empty device_info."
    )
    assert '"在线"' in body or "'在线'" in body, (
        "_device_status_text must return 在线 for populated device_info."
    )


def test_device_status_no_longer_reads_xml_field():
    """The XML ``deviceStatus`` field is no longer the source."""
    # Verify the device_status sensor uses the derived helper, not
    # ``system_status.get("deviceStatus")``.
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    # Pull the device_status sensor entry. Split at the next
    # HikvisionISAPISensorDescription( that starts at column 4 (i.e.
    # next tuple entry).
    section = src.split('key="device_status"', 1)[1]
    next_entry = section.find("\n    HikvisionISAPISensorDescription(")
    if next_entry > 0:
        section = section[:next_entry]
    # Should call _device_status_text(d), not system_status.get(...).
    assert "_device_status_text(d)" in section, (
        "v0.6.19: device_status sensor value_fn must call "
        "_device_status_text(d), not read system_status XML."
    )
    assert "system_status.get" not in section, (
        "v0.6.19: device_status sensor must NOT read system_status XML."
    )


# ---- new sensors ----


def test_v0619_adds_mtu_sensor():
    """The MTU sensor must be added (interface MTU)."""
    keys = _sensor_keys()
    assert "network_mtu" in keys, (
        "v0.6.19: must add the ``network_mtu`` sensor exposing "
        "the first interface's MTU."
    )


def test_v0619_adds_time_mode_sensor():
    """The ``time_mode`` sensor (NTP/manual) must be added."""
    keys = _sensor_keys()
    assert "time_mode" in keys, (
        "v0.6.19: must add the ``time_mode`` sensor exposing "
        "NTP vs manual from /ISAPI/System/time."
    )


def test_v0619_removes_encoder_release_date_sensor():
    """``encoder_release_date`` must be removed (V4-only, low value)."""
    keys = _sensor_keys()
    assert "encoder_release_date" not in keys, (
        "v0.6.19: ``encoder_release_date`` must be removed. It was "
        "V4-firmware-only and of low operational value vs "
        "``encoder_version``."
    )


def test_v0619_string_sensors_use_or_none():
    """String-reading sensors apply ``_or_none`` to suppress empty cells."""
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    # A few representative string sensors — every one must call _or_none
    # on its device_info/network/streaming-channel-detail string read.
    for snippet in (
        'd.device_info.get("model")',
        'd.device_info.get("serialNumber")',
        'd.device_info.get("firmwareVersion")',
        'd.device_info.get("encoderVersion")',
        'd.streaming_channel_detail.get("video_codec")',
        'd.streaming_channel_detail.get("audio_codec")',
        'd.time_info.get("time_mode")',
    ):
        # Find the line containing this snippet (truncated for context)
        line_prefix = snippet.split("(")[0]
        # Search: must appear inside a `_or_none(...)` call somewhere
        # in the file. We allow both .get("...") and other forms.
        assert (
            f"_or_none({snippet})" in src
            or f"_or_none(d.{snippet.split('d.')[-1]}" in src
        ), (
            f"v0.6.19: string sensor reading {snippet!r} must wrap "
            f"the read in ``_or_none(...)`` so empty strings become "
            f"``None`` instead of blank cells."
        )


# ---- binary_sensor: device_online ----


def test_v0619_device_online_class_defined():
    """binary_sensor.py must define ``HikvisionISAPIDeviceOnlineBinarySensor``."""
    src = _BINARY_SRC.read_text(encoding="utf-8-sig")
    assert "class HikvisionISAPIDeviceOnlineBinarySensor" in src, (
        "v0.6.19: must define ``HikvisionISAPIDeviceOnlineBinarySensor``."
    )
    # Must be CONNECTIVITY class (HA will render with the connectivity icon).
    section = src.split("HikvisionISAPIDeviceOnlineBinarySensor", 1)[1]
    assert "CONNECTIVITY" in section, (
        "device_online binary sensor must use BinarySensorDeviceClass.CONNECTIVITY."
    )


def test_v0619_device_online_registered():
    """async_setup_entry must register the device_online binary sensor."""
    src = _BINARY_SRC.read_text(encoding="utf-8-sig")
    # Take the full async_setup_entry body (between the def and the next
    # top-level def).
    setup = src.split("async def async_setup_entry", 1)[1].split(
        "\n\n\n", 1
    )[0]
    assert "HikvisionISAPIDeviceOnlineBinarySensor" in setup, (
        "async_setup_entry must register the device_online entity "
        "before iterating per-channel entities."
    )
    # It must appear before the per-channel entities (channel_{N}_*).
    online_pos = setup.find("HikvisionISAPIDeviceOnlineBinarySensor")
    channels_pos = setup.find("_entities_for_channel")
    assert online_pos < channels_pos, (
        "device_online must be appended to entities list BEFORE "
        "the per-channel iteration."
    )


# ---- button: PTZ directions ----


def test_v0619_ptz_button_class_defined():
    """button.py must define ``HikvisionISAPIPTZButton``."""
    src = _BUTTON_SRC.read_text(encoding="utf-8-sig")
    assert "class HikvisionISAPIPTZButton" in src, (
        "v0.6.19: must define the ``HikvisionISAPIPTZButton`` entity."
    )


def test_v0619_ptz_buttons_conditional_on_capability():
    """PTZ buttons registered only when capabilities["ptz"] is True."""
    src = _BUTTON_SRC.read_text(encoding="utf-8-sig")
    assert 'capabilities.get("ptz")' in src, (
        "v0.6.19: PTZ direction buttons must be registered only "
        "when ``capabilities['ptz']`` is True."
    )


def test_v0619_ptz_button_xml_body():
    """The PTZ button must PUT XML with the right shape to /PTZCtrl/.../continuous."""
    src = _BUTTON_SRC.read_text(encoding="utf-8-sig")
    # Required XML elements: <PTZData><continuous><direction>...<duration>...
    for tag in ("PTZData", "continuous", "direction", "duration"):
        assert f"<{tag}>" in src, (
            f"PTZ button XML body must include ``<{tag}>`` element."
        )
    assert "/ISAPI/PTZCtrl/channels/" in src, (
        "PTZ button must PUT to /ISAPI/PTZCtrl/channels/<n>/continuous."
    )


# ---- coordinator: MTU <Link>/MTU fallback ----


def test_v0619_mtu_falls_back_to_link():
    """_parse_network_interfaces must try <Link>/MTU before direct MTU."""
    src = _COORD_SRC.read_text(encoding="utf-8-sig")
    assert 'Link/MTU' in src, (
        "v0.6.19: _parse_network_interfaces must read MTU from "
        "<Link>/MTU (V5 shape) before falling back to direct MTU."
    )


def test_v0619_time_info_in_data():
    """HikvisionISAPIData must expose time_info; coordinator must populate it."""
    src = _COORD_SRC.read_text(encoding="utf-8-sig")
    assert "time_info:" in src, (
        "v0.6.19: HikvisionISAPIData must have a ``time_info`` field."
    )
    assert "_parse_time" in src, (
        "v0.6.19: coordinator must define ``_parse_time`` for "
        "/ISAPI/System/time XML."
    )
    assert "ISAPI_SYSTEM_TIME" in src, (
        "v0.6.19: coordinator must fetch ISAPI_SYSTEM_TIME."
    )