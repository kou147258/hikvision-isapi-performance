"""Regression tests for v0.6.23 — dual-NIC support.

User feedback after v0.6.22: "还有NVR和DVR有些是双网卡，
你这个只显示单网卡" (some NVR/DVRs are dual-NIC, you only show
single NIC).

V4 NVR ``DS-7708N-I4`` exposes two NICs (e.g. 10.18.176.65 +
192.168.10.10). Pre-v0.6.23 the sensor used ``_first_iface`` and
showed only the first NIC. v0.6.23 adds 5 NIC 2 sensors
(``network_2_ip`` / ``network_2_subnet`` / ``network_2_gateway``
/ ``network_2_mac`` / ``network_2_mtu``) registered only when
``len(network_interfaces) >= 2``.

Tests:
- 5 NIC 2 sensor keys exist in ``SENSORS``.
- 5 NIC 2 sensors use ``_second_iface`` (not ``_first_iface``).
- 5 NIC 2 sensors have distinct ``translation_key`` (NIC 1's
  key would re-render NIC 2 in the user's language as
  "Network IP Address" — confusing on a multi-NIC device).
- ``_second_iface`` returns the second interface's data when
  there are 2+ NICs; returns ``None`` for single-NIC.
- ``async_setup_entry`` filters NIC 2 sensors and only registers
  them when ``coordinator.network_interfaces`` has 2+ entries.
"""

from __future__ import annotations

from pathlib import Path


_SENSOR_SRC = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance\sensor.py"
)


def _sensor_keys() -> list[str]:
    import re
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    return re.findall(r'key="([a-zA-Z0-9_]+)"', src)


def _sensor_translation_keys() -> list[str]:
    import re
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    return re.findall(r'\n\s+translation_key="([^"]+)"', src)


# ---- NIC 2 sensor definitions exist ----


def test_v0623_nic2_sensors_present():
    """All 5 NIC 2 sensor keys must be defined."""
    keys = _sensor_keys()
    for k in (
        "network_2_ip",
        "network_2_subnet",
        "network_2_gateway",
        "network_2_mac",
        "network_2_mtu",
    ):
        assert k in keys, f"v0.6.23: NIC 2 sensor {k!r} missing"


def test_v0623_nic2_use_second_iface():
    """NIC 2 sensors must call ``_second_iface``, not ``_first_iface``.

    A pre-v0.6.23 copy-paste bug: I reused the NIC 1 lambda which
    returned the FIRST interface's data — defeating the whole
    purpose of having NIC 2 sensors.
    """
    import re

    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    # Pull each NIC 2 description block.
    for key in (
        "network_2_ip",
        "network_2_subnet",
        "network_2_gateway",
        "network_2_mac",
        "network_2_mtu",
    ):
        section = src.split(f'key="{key}"', 1)[1]
        next_entry = section.find("\n    HikvisionISAPISensorDescription(")
        if next_entry > 0:
            section = section[:next_entry]
        assert "_second_iface" in section, (
            f"v0.6.23: NIC 2 sensor {key!r} must use _second_iface, "
            f"not _first_iface. Pre-v0.6.23 copy-paste bug surfaced as "
            f"both NIC 1 and NIC 2 showing the same IP."
        )
        assert "_first_iface" not in section, (
            f"v0.6.23: NIC 2 sensor {key!r} must NOT call _first_iface "
            f"(it would always return the first NIC's data)."
        )


def test_v0623_nic2_translation_keys_distinct():
    """Each NIC 2 sensor must have a distinct ``translation_key``.

    Pre-fix, NIC 2 sensors reused ``network_ip`` / ``network_subnet``
    / etc. — both NIC 1 and NIC 2 would render in the user's
    language as the same string ("Network IP Address"), making the
    two NICs visually indistinguishable.
    """
    tkeys = _sensor_translation_keys()
    for k in (
        "network_2_ip",
        "network_2_subnet",
        "network_2_gateway",
        "network_2_mac",
        "network_2_mtu",
    ):
        assert k in tkeys, (
            f"v0.6.23: NIC 2 sensor must declare its own "
            f"translation_key {k!r} (not reuse NIC 1's key)."
        )
    # No duplicates overall (existing v0.6.5 test already checks
    # this, but make it explicit for the new keys too).
    duplicates = sorted({k for k in tkeys if tkeys.count(k) > 1})
    assert not duplicates, (
        f"translation_keys have duplicates: {duplicates}"
    )


# ---- _second_iface helper ----


def test_v0623_second_iface_returns_second_interface():
    """``_second_iface(data, field)`` returns data.network_interfaces[1].

    Implemented by reading the source and exec'ing the function body
    in a minimal namespace, since the conftest's
    ``SensorEntityDescription`` stub doesn't support dataclass
    inheritance (the real HA class IS a dataclass).
    """
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    # Include _or_none (used by both _first_iface and _second_iface),
    # then _first_iface and _second_iface. We need them all in one
    # shared namespace because _second_iface calls _or_none via
    # module-level lookup.
    body = ""
    for fn_name in ("_or_none", "_first_iface", "_second_iface"):
        start = src.find(f"def {fn_name}(")
        if start < 0:
            continue
        rest = src[start:]
        end = rest.find("\n\ndef ", 1)
        if end < 0:
            end = rest.find("\n\nclass ", 1)
        if end < 0:
            end = len(rest)
        body += "\n" + rest[:end]

    ns: dict = {"Any": object, "HikvisionISAPIData": type("Data", (), {})}
    exec(body, ns)

    class _Data:
        def __init__(self, ifaces):
            self.network_interfaces = ifaces

    data = _Data([
        {"ip_address": "10.18.176.65", "mac_address": "02:00:00:00:00:0e"},
        {"ip_address": "192.168.10.10", "mac_address": "02:00:00:00:00:0f"},
    ])
    assert ns["_second_iface"](data, "ip_address") == "192.168.10.10"
    assert ns["_second_iface"](data, "mac_address") == "02:00:00:00:00:0f"


def test_v0623_second_iface_returns_none_for_single_nic():
    """``_second_iface`` returns None when there's only one NIC."""
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    body = ""
    for fn_name in ("_or_none", "_first_iface", "_second_iface"):
        start = src.find(f"def {fn_name}(")
        if start < 0:
            continue
        rest = src[start:]
        end = rest.find("\n\ndef ", 1)
        if end < 0:
            end = rest.find("\n\nclass ", 1)
        if end < 0:
            end = len(rest)
        body += "\n" + rest[:end]
    ns: dict = {"Any": object, "HikvisionISAPIData": type("Data", (), {})}
    exec(body, ns)

    class _Data:
        def __init__(self, ifaces):
            self.network_interfaces = ifaces

    data = _Data([{"ip_address": "10.18.176.65"}])
    assert ns["_second_iface"](data, "ip_address") is None


def test_v0623_second_iface_handles_empty_string():
    """``_second_iface`` returns None for empty-string fields (via _or_none)."""
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    body = ""
    for fn_name in ("_or_none", "_first_iface", "_second_iface"):
        start = src.find(f"def {fn_name}(")
        if start < 0:
            continue
        rest = src[start:]
        end = rest.find("\n\ndef ", 1)
        if end < 0:
            end = rest.find("\n\nclass ", 1)
        if end < 0:
            end = len(rest)
        body += "\n" + rest[:end]
    ns: dict = {"Any": object, "HikvisionISAPIData": type("Data", (), {})}
    exec(body, ns)

    class _Data:
        def __init__(self, ifaces):
            self.network_interfaces = ifaces

    data = _Data([
        {"ip_address": "10.18.176.65"},
        {"ip_address": ""},
    ])
    assert ns["_second_iface"](data, "ip_address") is None


# ---- async_setup_entry: NIC 2 conditional registration ----


def test_v0623_async_setup_entry_gates_nic2_on_interface_count():
    """``async_setup_entry`` must check ``len(network_interfaces) >= 2``
    before registering NIC 2 sensors.

    Single-NIC devices (most IPCs, single-NIC NVRs) should NOT
    show 5 NIC 2 entities that would all be permanently unknown.
    """
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    body_start = src.find("async def async_setup_entry(")
    assert body_start > 0
    # The async_setup_entry body extends to the next top-level def/class/@.
    rest = src[body_start:]
    boundary = None
    for marker in ("\n\ndef ", "\n\nclass ", "\n\n@"):
        pos = rest.find(marker, 1)
        if pos > 0 and (boundary is None or pos < boundary):
            boundary = pos
    body = rest[:boundary] if boundary else rest

    assert "network_interfaces" in body, (
        "v0.6.23: async_setup_entry must inspect coordinator.network_interfaces "
        "to decide whether to register NIC 2 sensors."
    )
    assert "network_2_" in body or "_make_nic2_listener" in body, (
        "v0.6.23: async_setup_entry must reference NIC 2 sensors "
        "(either synchronously or via the late-arrival listener)."
    )