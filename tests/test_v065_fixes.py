"""Tests for the v0.6.5 fix batch.

Covers regressions found during integration testing:

1. **Coordinator NameError on ISAPIAuthError** — the coordinator
   referenced ``ISAPIAuthError`` in an ``except`` clause but only
   imported ``ISAPIConnectionError`` / ``ISAPIError``. Any 401 / 403
   from a real Hikvision device (after the digest handshake fails)
   raised ``NameError: name 'ISAPIAuthError' is not defined``
   instead of being converted to ``UpdateFailed``.

2. **Duplicate sensor unique_ids** — the SENSORS tuple in
   ``sensor.py`` had two pairs with the same ``key``:
   ``device_uptime`` (one sourced from /ISAPI/System/status, one
   from per-channel status) and ``reboot_count`` (same pattern).
   Each pair collided on ``{entry_id}_{key}`` and HA logged
   "does not generate unique IDs - ignoring sensor.X". Fixed by
   renaming the per-channel variants to ``channel_1_uptime`` /
   ``channel_1_reboot_count``.
"""

from __future__ import annotations

import re
from pathlib import Path


# ---- coordinator import ----


def test_coordinator_imports_ISAPIAuthError():
    """v0.6.5 fix: coordinator must import ISAPIAuthError for the except clause."""
    from pathlib import Path as _P
    src = (_P(__file__).parent.parent
           / "custom_components" / "hikvision_isapi_performance"
           / "coordinator.py").read_text(encoding="utf-8-sig")
    m = re.search(r"^from \.isapi_client import (.+)$", src, re.MULTILINE)
    assert m is not None, "coordinator.py missing isapi_client import"
    imported = m.group(1)
    assert "ISAPIAuthError" in imported, (
        "coordinator.py does not import ISAPIAuthError — "
        "the except clause on line 569 would NameError on a real 401/403."
    )


def test_coordinator_except_clause_references_ISAPIAuthError():
    """The except clause that converts auth errors to UpdateFailed must exist."""
    src = (Path(__file__).parent.parent
           / "custom_components" / "hikvision_isapi_performance"
           / "coordinator.py").read_text(encoding="utf-8-sig")
    assert "except ISAPIAuthError as exc" in src, (
        "coordinator.py lost its ISAPIAuthError except clause"
    )


# ---- sensor unique IDs ----


def _sensor_keys_from_source() -> list[str]:
    """Extract every ``key="..."`` literal from sensor.py.

    We use source-level parsing because importing the integration
    requires a full HA install or extensive stubbing — neither is
    needed for this regression check.
    """
    src = (Path(__file__).parent.parent
           / "custom_components" / "hikvision_isapi_performance"
           / "sensor.py").read_text(encoding="utf-8-sig")
    return re.findall(r'\n\s+key="([^"]+)"', src)


def _sensor_translation_keys_from_source() -> list[str]:
    """Extract every ``translation_key="..."`` literal from sensor.py."""
    src = (Path(__file__).parent.parent
           / "custom_components" / "hikvision_isapi_performance"
           / "sensor.py").read_text(encoding="utf-8-sig")
    return re.findall(r'\n\s+translation_key="([^"]+)"', src)


def test_sensor_descriptions_have_unique_keys():
    """v0.6.5 fix: every HikvisionISAPISensorDescription.key is unique.

    Pre-fix, ``device_uptime`` and ``reboot_count`` appeared twice
    (once from /ISAPI/System/status, once from per-channel status).
    Both collided on the unique_id derived from the key and HA
    silently dropped the second one with a warning.
    """
    keys = _sensor_keys_from_source()
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert not duplicates, (
        f"Sensor description keys have duplicates: {duplicates}. "
        f"All keys: {keys}"
    )


def test_sensor_descriptions_have_unique_translation_keys():
    """translation_key must also be unique (it's used by HA's i18n lookup)."""
    tkeys = _sensor_translation_keys_from_source()
    duplicates = sorted({k for k in tkeys if tkeys.count(k) > 1})
    assert not duplicates, (
        f"Sensor description translation_keys have duplicates: {duplicates}. "
        f"All translation_keys: {tkeys}"
    )


def test_v065_channel_1_sensors_removed_in_v0617():
    """v0.6.17 cleanup removed the per-channel-derived sensors
    (``channel_1_uptime``, ``channel_1_reboot_count``,
    ``sd_card_writes``, ``dome_high_temp_runtime``) because the
    V4 NVR in the user's fleet doesn't serve the per-channel
    status endpoint, so these were always "unknown" in their
    HA UI. The original v0.6.5 fix (preventing duplicate keys)
    is moot after the deletion — replace with a structural
    check that the sensors are no longer declared.
    """
    keys = set(_sensor_keys_from_source())
    # As of v0.6.17 these are intentionally absent. A future
    # re-add would need to come with namespace-safe unique_id
    # handling (the v0.6.5 fix).
    assert "channel_1_uptime" not in keys
    assert "channel_1_reboot_count" not in keys
    assert "sd_card_writes" not in keys
    assert "dome_high_temp_runtime" not in keys


def test_v0617_device_uptime_seconds_sensor_removed():
    """v0.6.17 cleanup removed the seconds-based uptime sensors;
    only the hours version remains. The v0.6.5 fix needed them
    to be unique, but now they're gone entirely so the test
    is a structural check that ``device_uptime`` and ``uptime``
    (seconds) aren't reintroduced."""
    src = (Path(__file__).parent.parent
           / "custom_components" / "hikvision_isapi_performance"
           / "sensor.py").read_text(encoding="utf-8-sig")
    # ``device_uptime`` was a distinct entity in v0.6.5 with a
    # duplicate per-channel twin; v0.6.17 deleted both and only
    # ``uptime_hours`` remains.
    assert len(re.findall(r'\n\s+key="device_uptime"', src)) == 0, (
        "sensor.py still declares a `device_uptime` sensor — "
        "v0.6.17 removed it in favor of `uptime_hours`."
    )
    assert len(re.findall(r'\n\s+key="uptime"', src)) == 0, (
        "sensor.py still declares a plain `uptime` (seconds) "
        "sensor — v0.6.17 removed it in favor of `uptime_hours`."
    )


def test_device_uptime_and_reboot_count_have_single_definition():
    """The device-level ``reboot_count`` appears exactly once."""
    keys = _sensor_keys_from_source()
    assert keys.count("reboot_count") == 1, f"got {keys}"


# ---- regression: source-level guard against reintroducing duplicates ----


def test_sensor_source_has_no_duplicate_key_string():
    """Source-level regression guard: the literal ``key="reboot_count"``
    (the only remaining duplicate-prone sensor after v0.6.17) appears
    exactly once in the source.
    """
    src = (Path(__file__).parent.parent
           / "custom_components" / "hikvision_isapi_performance"
           / "sensor.py").read_text(encoding="utf-8-sig")
    # Use re to avoid matching ``translation_key="..."`` which has the
    # same suffix.
    assert len(re.findall(r'\n\s+key="reboot_count"', src)) == 1, (
        "sensor.py has more than one ``key=\"reboot_count\"`` literal"
    )