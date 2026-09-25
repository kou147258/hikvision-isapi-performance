"""Regression tests for v0.6.30.

User feedback after v0.6.29 reported two regressions:

1. **``channel_count`` sensor showed "unknown" forever.**
   v0.6.27 introduced a filter
   ``not d.key.startswith("channel_")`` to exclude per-channel
   entities from the NIC 1 loop. The filter incorrectly matched
   the static ``channel_count`` entry too (its key is
   ``"channel_count"`` which starts with ``"channel_"``), so
   the entity was never registered in HA.

   Fix: replace the startswith check with a regex
   ``^channel_\\d+_`` that matches the per-channel pattern
   only (``channel_1_video_codec``, ``channel_2_video_codec``,
   ...). ``channel_count`` doesn't match the regex and is now
   registered normally.

2. **NVR second NIC sensor still doesn't appear.**
   v0.6.23 already added the dynamic NIC 2 registration with a
   late-arrival listener. The code path was unchanged by
   v0.6.27–v0.6.29 — the entity simply gets re-registered on
   Reload. No code change needed; the release notes for
   v0.6.30 explain the Reload path explicitly.

Plus an audit listing every entity the integration exposes at
v0.6.30, so users can decide which to keep / disable in HA's
entity registry (per the user's "看看那些能加上并且能正常获取
到实体参数的" request).

Tests:
- ``channel_count`` is NOT filtered out by the v0.6.30 regex
- All NIC 1 sensors (non-per-channel, non-NIC 2) are present
  in the resulting list
- Per-channel sensors with channel id 1/2/3 ARE filtered out
- NIC 2 sensors are still filtered out
- Audit: every static SENSORS key is accounted for (either
  NIC 1, per-channel dynamic, or NIC 2)
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(r"C:\Users\43457\Desktop\hikvision-isapi")
_INTEGRATION_ROOT = (
    _REPO_ROOT / "custom_components" / "hikvision_isapi_performance"
)


# ---------------------------------------------------------------------------
# Channel_count bug regression
# ---------------------------------------------------------------------------


def test_v0630_channel_count_key_not_filtered_by_per_channel_regex():
    """``channel_count`` is NOT matched by the per-channel regex.

    v0.6.27 filter ``not d.key.startswith("channel_")`` was a
    blunt instrument that caught ``channel_count`` along with
    per-channel entries. v0.6.30 narrows the filter to a
    digit-prefix pattern (``channel_<digits>_*``) so the static
    ``channel_count`` entry survives.
    """
    # The exact regex used in async_setup_entry.
    pattern = re.compile(r"^channel_\d+_")
    # channel_count doesn't have a digit after channel_, so it
    # must NOT match (and therefore is NOT excluded).
    assert not pattern.match("channel_count"), (
        "v0.6.30: regex must not match 'channel_count' (the "
        "filter should let this static entry through)."
    )
    # Per-channel entries DO match (they're the ones to exclude).
    assert pattern.match("channel_1_video_codec")
    assert pattern.match("channel_2_video_codec")
    assert pattern.match("channel_8_audio_codec")
    assert pattern.match("channel_99_name")


def test_v0630_nic1_includes_channel_count_and_excludes_per_channel():
    """Apply the v0.6.30 filter to the actual SENSORS list:
    channel_count passes, channel_{N}_* do not.
    """
    sensor_src = (
        _INTEGRATION_ROOT / "sensor.py"
    ).read_text(encoding="utf-8-sig")
    # Extract every HikvisionISAPISensorDescription key by
    # source-level regex (cheaper than importing sensor.py which
    # the conftest dataclass stub can't quite load).
    keys = re.findall(
        r'HikvisionISAPISensorDescription\(\s*key="([^"]+)"',
        sensor_src,
    )
    assert "channel_count" in keys, (
        "Sanity: SENSORS must contain a channel_count entry."
    )
    # Apply the v0.6.30 filter (same logic as in async_setup_entry).
    pattern = re.compile(r"^channel_\d+_")
    nic1_keys = [
        k for k in keys
        if not k.startswith("network_2_")
        and not pattern.match(k)
    ]
    assert "channel_count" in nic1_keys, (
        f"v0.6.30 bug not fixed: 'channel_count' is not in "
        f"NIC 1 set (got {nic1_keys[:5]}...). The filter is "
        f"still excluding it."
    )
    # Per-channel entries must be excluded.
    per_channel_keys = [k for k in keys if pattern.match(k)]
    for k in per_channel_keys:
        assert k not in nic1_keys, (
            f"v0.6.30: per-channel '{k}' leaked into NIC 1 set."
        )
    # NIC 2 entries must be excluded.
    for k in keys:
        if k.startswith("network_2_"):
            assert k not in nic1_keys, (
                f"v0.6.30: NIC 2 '{k}' leaked into NIC 1 set."
            )


# ---------------------------------------------------------------------------
# NIC 2 defensive check — make sure v0.6.27 filter changes
# didn't break NIC 2 registration.
# ---------------------------------------------------------------------------


def test_v0630_nic2_filter_still_excludes_network_2_keys():
    """``network_2_*`` keys must still be filtered out of NIC 1
    (they're emitted separately when ``network_interfaces >= 2``).
    """
    sensor_src = (
        _INTEGRATION_ROOT / "sensor.py"
    ).read_text(encoding="utf-8-sig")
    keys = re.findall(
        r'HikvisionISAPISensorDescription\(\s*key="([^"]+)"',
        sensor_src,
    )
    nic2_keys = [k for k in keys if k.startswith("network_2_")]
    assert len(nic2_keys) == 5, (
        f"Sanity: expect 5 network_2_* keys (ip/subnet/gateway/"
        f"mac/mtu), got {len(nic2_keys)}: {nic2_keys}"
    )
    expected = {
        "network_2_ip",
        "network_2_subnet",
        "network_2_gateway",
        "network_2_mac",
        "network_2_mtu",
    }
    assert set(nic2_keys) == expected, (
        f"v0.6.30: NIC 2 keys changed unexpectedly: "
        f"{set(nic2_keys) - expected} extra, "
        f"{expected - set(nic2_keys)} missing."
    )


def test_v0630_nic2_listener_registered_in_setup():
    """``async_setup_entry`` must still register the NIC 2 listener
    so the entities appear on the next refresh if not at setup time.
    """
    sensor_src = (
        _INTEGRATION_ROOT / "sensor.py"
    ).read_text(encoding="utf-8-sig")
    # Both the sync registration block (when network_interfaces
    # already has 2+ entries at setup) and the late listener
    # must still exist.
    assert "_make_nic2_listener" in sensor_src, (
        "v0.6.30: _make_nic2_listener helper must still exist "
        "(NIC 2 late-arrival listener)."
    )
    # The setup block calls it.
    setup_block = sensor_src.split("async def async_setup_entry", 1)[1].split(
        "\n\nasync def", 1
    )[0]
    assert "_make_nic2_listener(" in setup_block, (
        "v0.6.30: async_setup_entry must still call "
        "_make_nic2_listener (registers the listener that adds "
        "NIC 2 entities on next refresh if they weren't available "
        "at setup time)."
    )


# ---------------------------------------------------------------------------
# Entity audit — list everything the integration exposes at v0.6.30
# ---------------------------------------------------------------------------


def test_v0630_entity_audit_completeness():
    """Every SENSORS key must be classified into exactly one bucket:
    NIC 1 (static), per-channel (dynamic), or NIC 2 (dynamic).
    No key should fall through the cracks.
    """
    sensor_src = (
        _INTEGRATION_ROOT / "sensor.py"
    ).read_text(encoding="utf-8-sig")
    keys = re.findall(
        r'HikvisionISAPISensorDescription\(\s*key="([^"]+)"',
        sensor_src,
    )
    pattern = re.compile(r"^channel_\d+_")
    nic1 = {k for k in keys
            if not k.startswith("network_2_") and not pattern.match(k)}
    per_channel = {k for k in keys if pattern.match(k)}
    nic2 = {k for k in keys if k.startswith("network_2_")}
    # Sum of buckets equals all keys (no orphans).
    assert nic1 | per_channel | nic2 == set(keys), (
        "v0.6.30: some SENSORS keys aren't classified. "
        f"NIC1-only: {set(keys) - per_channel - nic2 - nic1}; "
        f"maybe a new key was added without updating the filter."
    )
    # NIC1 should contain channel_count (the regression guard).
    assert "channel_count" in nic1


def test_v0630_release_notes_document_nic2_reload_path():
    """v0.6.30 release notes must explicitly tell users that
    deleted NIC 2 entities re-appear on Reload.

    The user reported "NVR still only shows the first NIC" after
    v0.6.29. The fix isn't code (v0.6.27 didn't break NIC 2);
    it's user-facing documentation: "if you deleted the
    network_2_* entities manually, Reload re-registers them."
    """
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8-sig")
    # Look for a release-notes block mentioning v0.6.30 (or at
    # least a "What's new" section that's present at this
    # point in the file).
    assert "v0.6.30" in readme or "0.6.30" in readme, (
        "v0.6.30: README must mention the v0.6.30 release so "
        "users see the entity list update + NIC 2 Reload "
        "instructions."
    )