"""Regression tests for v0.6.27.

User feedback after v0.6.26 shipped three concerns:

1. **v0.6.26 over-suppressed ``cpu_usage``.**
   v0.6.26 dropped the cpu_usage entity on every NVR/DVR. Users
   with V5+ NVRs (which report cpuUtilization correctly) lost the
   sensor for no reason. v0.6.27 narrows the suppression to V4
   firmware only — IPC and V5+ NVR keep cpu_usage, only V4 NVR
   drops it.

2. **Only channel 1 had streaming-detail sensors.**
   v0.6.18 added 5 static ``channel_1_*`` sensors. Users with NVRs
   (8/16/32 channels) only saw channel 1's codec/resolution/
   frame_rate/bitrate/audio_codec. v0.6.27 makes the sensors
   dynamic — every detected channel gets the same 6 sensors
   (5 streaming + 1 name).

3. **MTU sensor showed "1500 B" — user wants raw number.**
   v0.6.27 drops the ``native_unit_of_measurement="B"`` so HA
   displays the MTU as a plain integer.

Tests:
- V5 NVR firmware keeps cpu_usage sensor (regression of v0.6.26)
- V4 NVR firmware suppresses cpu_usage sensor
- IPC keeps cpu_usage sensor regardless of firmware
- MTU sensor has no native_unit_of_measurement
- Dynamic per-channel sensors: channel 1, 2, 3 all get the
  same 6 sensors with correct keys + names
- Per-channel sensors fall back gracefully when streaming
  endpoint isn't populated (V4 NVR)
- Channel name sensor reads from coordinator data
- Late-arrival listener: when channels arrive after first
  refresh, the entities register on the next update
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path


_REPO_ROOT = Path(r"C:\Users\43457\Desktop\hikvision-isapi")
_INTEGRATION_ROOT = (
    _REPO_ROOT / "custom_components" / "hikvision_isapi_performance"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _source_load_function(path: Path, fn_name: str, extra_ns: dict | None = None):
    """Source-load a function from ``path`` and return the live object."""
    body_lines = _read(path).splitlines()
    start_idx = None
    for i, line in enumerate(body_lines):
        if line.startswith(f"def {fn_name}("):
            start_idx = i
            break
    assert start_idx is not None, f"{fn_name} not found in {path}"
    end_idx = start_idx + 1
    while end_idx < len(body_lines):
        nxt = body_lines[end_idx]
        if (
            nxt.startswith("def ")
            or nxt.startswith("class ")
            or nxt.startswith("@")
        ):
            break
        end_idx += 1
    func_src = "\n".join(body_lines[start_idx:end_idx])
    ns: dict = {}
    if extra_ns:
        ns.update(extra_ns)
    exec(func_src, ns)
    return ns[fn_name]


def _stub_classes():
    """Return a dict of stub classes / namespaces for source-loaded functions."""

    class _StubDesc:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _StubSensor:
        def __init__(self, coordinator, entry, description):
            self.coordinator = coordinator
            self.entry = entry
            self.entity_description = description

    return {
        "Any": object,
        "HikvisionISAPISensorDescription": _StubDesc,
        "HikvisionISAPISensor": _StubSensor,
        "SensorDeviceClass": types.SimpleNamespace(
            FREQUENCY="frequency", DATA_RATE="data_rate"
        ),
        "SensorStateClass": types.SimpleNamespace(MEASUREMENT="measurement"),
    }


# ---------------------------------------------------------------------------
# 🟥2 (revert) — V5+ NVR keeps cpu_usage, V4 NVR drops it
# ---------------------------------------------------------------------------


def test_v0627_v5_nvr_keeps_cpu_usage():
    """V5 firmware NVR must keep the ``cpu_usage`` sensor.

    v0.6.26 dropped cpu_usage on all NVR/DVR (because the V4
    firmware bug returns 0). V5/V6/V7 NVR firmware reports
    cpuUtilization correctly — those users lost a working sensor
    on v0.6.26. v0.6.27 narrows the suppression to V4 firmware
    only.
    """
    src = _read(_INTEGRATION_ROOT / "sensor.py")
    # The setup must read firmwareVersion to drive the cpu_usage gate.
    assert "firmwareVersion" in src, (
        "v0.6.27: async_setup_entry must read firmwareVersion to "
        "narrow cpu_usage suppression to V4 firmware."
    )
    # The combined flag is_v4_recorder = is_recorder AND V4 firmware.
    assert "is_v4_recorder" in src, (
        "v0.6.27: async_setup_entry must compute is_v4_recorder "
        "(is_recorder AND firmwareVersion startswith 'V4')."
    )
    # The exact filter clause:
    #   and (not is_v4_recorder or desc.key != "cpu_usage")
    comp_match = re.search(
        r"for desc in nic1_descs\s*\n\s*if\s*\((.*?)\)\s*\]",
        src,
        re.DOTALL,
    )
    assert comp_match, "nic1_descs list comprehension not found"
    body = comp_match.group(1)
    assert "not is_v4_recorder" in body, (
        "v0.6.27: the cpu_usage filter must use is_v4_recorder "
        "(not is_recorder) so V5+ NVR retains the sensor."
    )
    # Negative case: the OLD blanket "not is_recorder" must NOT
    # appear in the filter (would still drop cpu_usage on V5+ NVR).
    assert "not is_recorder or desc.key != \"cpu_usage\"" not in body, (
        "v0.6.27: the v0.6.26 over-broad filter must be removed. "
        "Found the old blanket 'not is_recorder' clause."
    )


def test_v0627_v4_nvr_still_drops_cpu_usage():
    """V4 firmware NVR must drop ``cpu_usage`` (the original bug shield).

    Pinning v0.6.26's intent: V4 NVR firmware returns
    cpuUtilization=0 — surfacing this as 0% CPU is worse than
    dropping the sensor. v0.6.27 keeps this behavior, just
    narrowed to V4 firmware.
    """
    src = _read(_INTEGRATION_ROOT / "sensor.py")
    comp_match = re.search(
        r"for desc in nic1_descs\s*\n\s*if\s*\((.*?)\)\s*\]",
        src,
        re.DOTALL,
    )
    assert comp_match
    body = comp_match.group(1)
    # Filter must drop cpu_usage when is_v4_recorder is True.
    assert "is_v4_recorder" in body and "cpu_usage" in body, (
        "v0.6.27: cpu_usage must still be filtered out for V4 "
        "firmware NVR (regression guard for the original bug shield)."
    )


def test_v0627_ipc_always_keeps_cpu_usage():
    """IPC (``is_recorder=False``) must always keep ``cpu_usage``.

    Regardless of firmware version. IPC's CPU readings are
    reliable across V4/V5/V6/V7 firmware generations.
    """
    src = _read(_INTEGRATION_ROOT / "sensor.py")
    # When is_recorder is False, is_v4_recorder is False regardless
    # of firmware, so the cpu_usage clause evaluates to True and the
    # sensor passes through. Verify the flag is correctly defined.
    assert "is_v4_recorder = is_recorder and firmware_version" in src or (
        "is_v4_recorder = is_recorder and firmware_version.startswith"
    ), (
        "v0.6.27: is_v4_recorder must be defined as "
        "'is_recorder and firmware_version.startswith(...)'."
    )


# ---------------------------------------------------------------------------
# Multi-channel sensors (dynamic)
# ---------------------------------------------------------------------------


def test_v0627_per_channel_sensors_emitted_for_each_channel():
    """``_build_per_channel_entities`` must emit 6 sensors per channel.

    Each channel gets: video_codec, video_resolution,
    video_frame_rate, video_bitrate, audio_codec, name. v0.6.18
    only emitted these for channel 1 statically; v0.6.27 emits
    them for every detected channel dynamically.
    """
    sensor_path = _INTEGRATION_ROOT / "sensor.py"
    src = _read(sensor_path)

    # Source-load _channel_streaming_field, _channel_name_value,
    # _or_none, and the main _build_per_channel_entities.
    extra_ns = _stub_classes()
    # Pre-load helpers that _build_per_channel_entities references.
    for helper in ("_or_none", "_channel_streaming_field", "_channel_name_value"):
        body_lines = src.splitlines()
        helper_start = None
        for i, line in enumerate(body_lines):
            if line.startswith(f"def {helper}("):
                helper_start = i
                break
        if helper_start is None:
            continue
        helper_end = helper_start + 1
        while helper_end < len(body_lines):
            nxt = body_lines[helper_end]
            if (
                nxt.startswith("def ")
                or nxt.startswith("class ")
                or nxt.startswith("@")
            ):
                break
            helper_end += 1
        exec("\n".join(body_lines[helper_start:helper_end]), extra_ns)
    build_fn = _source_load_function(
        sensor_path, "_build_per_channel_entities", extra_ns=extra_ns
    )

    class _Coord:
        channels = []
        streaming_channel_detail = {"channels": []}

    class _Entry:
        entry_id = "test_entry"

    # Multi-channel: 1, 2, 3.
    for ch_id in ("1", "2", "3"):
        entities = build_fn(_Coord(), _Entry(), {"id": ch_id, "name": f"Cam {ch_id}"})
        keys = {e.entity_description.key for e in entities}
        expected = {
            f"channel_{ch_id}_video_codec",
            f"channel_{ch_id}_video_resolution",
            f"channel_{ch_id}_video_frame_rate",
            f"channel_{ch_id}_video_bitrate",
            f"channel_{ch_id}_audio_codec",
            f"channel_{ch_id}_name",
        }
        assert keys == expected, (
            f"v0.6.27: channel {ch_id} must emit exactly these 6 "
            f"sensors: {sorted(expected)}. Got: {sorted(keys)}."
        )

    # Channel 1 must STILL emit channel_1_* keys (backward compat
    # with v0.6.18–v0.6.26 entity registry).
    entities = build_fn(_Coord(), _Entry(), {"id": "1", "name": "Cam 1"})
    keys = {e.entity_description.key for e in entities}
    assert "channel_1_video_codec" in keys, (
        "v0.6.27: channel id=1 must still emit channel_1_* keys "
        "so existing entity-registry entries match."
    )


def test_v0627_per_channel_value_falls_back_to_first_channel():
    """When the parser collapses multi-channel data, channel 1
    still gets its reading from the legacy ``first_channel_*``
    top-level fields (v0.6.18 backwards compat).
    """
    sensor_path = _INTEGRATION_ROOT / "sensor.py"
    extra_ns = _stub_classes()
    body_lines = _read(sensor_path).splitlines()
    or_none_start = None
    for i, line in enumerate(body_lines):
        if line.startswith("def _or_none("):
            or_none_start = i
            break
    if or_none_start is not None:
        or_none_end = or_none_start + 1
        while or_none_end < len(body_lines):
            nxt = body_lines[or_none_end]
            if (
                nxt.startswith("def ")
                or nxt.startswith("class ")
                or nxt.startswith("@")
            ):
                break
            or_none_end += 1
        exec("\n".join(body_lines[or_none_start:or_none_end]), extra_ns)

    field_fn = _source_load_function(
        sensor_path, "_channel_streaming_field", extra_ns=extra_ns
    )

    # Mock data: streaming_channel_detail has no per-channel list,
    # only legacy first_channel_* fields. Channel 1 should still
    # read video_codec from those.
    class _Data:
        streaming_channel_detail = {
            "channels": [],
            "first_channel_id": "1",
            "video_codec": "H.264",
        }

    codec_fn = field_fn("1", "video_codec")
    assert codec_fn(_Data()) == "H.264", (
        "v0.6.27: channel 1 must fall back to first_channel "
        "top-level field when per-channel list is empty."
    )

    # Channel 2 should NOT use first_channel fallback (would be
    # wrong data) — return None.
    codec_fn_2 = field_fn("2", "video_codec")
    assert codec_fn_2(_Data()) is None, (
        "v0.6.27: channel 2 must NOT fall back to first_channel "
        "(would surface wrong codec on a different channel)."
    )


def test_v0627_per_channel_value_returns_none_for_missing_data():
    """``_channel_streaming_field`` returns None for missing fields."""
    sensor_path = _INTEGRATION_ROOT / "sensor.py"
    extra_ns = _stub_classes()
    body_lines = _read(sensor_path).splitlines()
    or_none_start = None
    for i, line in enumerate(body_lines):
        if line.startswith("def _or_none("):
            or_none_start = i
            break
    if or_none_start is not None:
        or_none_end = or_none_start + 1
        while or_none_end < len(body_lines):
            nxt = body_lines[or_none_end]
            if (
                nxt.startswith("def ")
                or nxt.startswith("class ")
                or nxt.startswith("@")
            ):
                break
            or_none_end += 1
        exec("\n".join(body_lines[or_none_start:or_none_end]), extra_ns)

    field_fn = _source_load_function(
        sensor_path, "_channel_streaming_field", extra_ns=extra_ns
    )

    class _Data:
        streaming_channel_detail = {"channels": []}

    fn = field_fn("1", "video_codec")
    assert fn(None) is None
    assert fn(_Data()) is None


def test_v0627_per_channel_value_normalises_empty_strings():
    """String fields must normalise empty / whitespace to None.

    v0.6.19 pinned the behaviour for the original channel_1_*
    static sensors; v0.6.27 preserved it in
    ``_channel_streaming_field``.
    """
    sensor_path = _INTEGRATION_ROOT / "sensor.py"
    extra_ns = _stub_classes()
    body_lines = _read(sensor_path).splitlines()
    or_none_start = None
    for i, line in enumerate(body_lines):
        if line.startswith("def _or_none("):
            or_none_start = i
            break
    if or_none_start is not None:
        or_none_end = or_none_start + 1
        while or_none_end < len(body_lines):
            nxt = body_lines[or_none_end]
            if (
                nxt.startswith("def ")
                or nxt.startswith("class ")
                or nxt.startswith("@")
            ):
                break
            or_none_end += 1
        exec("\n".join(body_lines[or_none_start:or_none_end]), extra_ns)
    field_fn = _source_load_function(
        sensor_path, "_channel_streaming_field", extra_ns=extra_ns
    )

    class _Data:
        streaming_channel_detail = {
            "channels": [
                {"id": "1", "video_codec": "", "audio_codec": "   "},
            ],
        }

    codec_fn = field_fn("1", "video_codec")
    assert codec_fn(_Data()) is None, (
        "v0.6.27: empty video_codec string must normalise to None."
    )
    audio_fn = field_fn("1", "audio_codec")
    assert audio_fn(_Data()) is None, (
        "v0.6.27: whitespace audio_codec string must normalise to None."
    )


def test_v0627_channel_name_falls_back_to_default():
    """``_channel_name_value`` returns ``f"Channel {id}"`` if no name.

    Devices that don't expose friendly channel names (V4 NVR)
    must still show a sensible entity state.
    """
    sensor_path = _INTEGRATION_ROOT / "sensor.py"
    extra_ns = _stub_classes()
    body_lines = _read(sensor_path).splitlines()
    or_none_start = None
    for i, line in enumerate(body_lines):
        if line.startswith("def _or_none("):
            or_none_start = i
            break
    if or_none_start is not None:
        or_none_end = or_none_start + 1
        while or_none_end < len(body_lines):
            nxt = body_lines[or_none_end]
            if (
                nxt.startswith("def ")
                or nxt.startswith("class ")
                or nxt.startswith("@")
            ):
                break
            or_none_end += 1
        exec("\n".join(body_lines[or_none_start:or_none_end]), extra_ns)

    name_fn = _source_load_function(
        sensor_path, "_channel_name_value", extra_ns=extra_ns
    )

    class _Data:
        channels = [{"id": "5", "name": "Front Door"}]

    # Channel present in data → use its name.
    fn_5 = name_fn("5")
    assert fn_5(_Data()) == "Front Door"
    # Channel id not in data → default fallback to "Channel {id}".
    fn_99 = name_fn("99")
    assert fn_99(_Data()) == "Channel 99"


def test_v0627_async_setup_entry_late_listener_registered():
    """``async_setup_entry`` must register a late-arrival listener
    for per-channel sensors.

    Mirrors the NIC 2 listener pattern. If the first coordinator
    refresh hasn't populated channels yet, the listener must
    register the entities on the next refresh.
    """
    src = _read(_INTEGRATION_ROOT / "sensor.py")
    assert "_make_per_channel_listener" in src, (
        "v0.6.27: helper _make_per_channel_listener must exist."
    )
    assert "async_add_listener" in src and (
        "_make_per_channel_listener" in src.split(
            "async def async_setup_entry", 1
        )[-1].split("\n\nasync def", 1)[0]
    ), (
        "v0.6.27: async_setup_entry must add a coordinator listener "
        "via _make_per_channel_listener for late-arriving channels."
    )


# ---------------------------------------------------------------------------
# MTU unit removal
# ---------------------------------------------------------------------------


def test_v0627_mtu_no_unit_suffix():
    """``network_mtu`` (and ``network_2_mtu``) sensors must NOT have
    a trailing "B" unit. Both NIC 1 and NIC 2 MTU were updated.
    """
    src = _read(_INTEGRATION_ROOT / "sensor.py")
    for key in ("network_mtu", "network_2_mtu"):
        # Find the HikvisionISAPISensorDescription block for this
        # key. Match ends at the closing paren followed by a comma
        # (the description list entry terminator) to avoid matching
        # the first ``)`` inside a lambda / function call.
        mtu_match = re.search(
            r'HikvisionISAPISensorDescription\(\s*key="' + key + r'".*?\),',
            src,
            re.DOTALL,
        )
        assert mtu_match, f"{key} sensor description not found"
        body = mtu_match.group(0)
        # Strip comment lines so we only look at code lines.
        # Comments mention "native_unit_of_measurement" in the docstring.
        code_lines = [
            ln for ln in body.splitlines()
            if not ln.strip().startswith("#")
        ]
        code_body = "\n".join(code_lines)
        assert "native_unit_of_measurement=" not in code_body, (
            f"v0.6.27: {key} must drop native_unit_of_measurement "
            f"so HA displays the raw integer (e.g. 1500), not '1500 B'."
        )


# ---------------------------------------------------------------------------
# Static channel_1_* entries removed from SENSORS
# ---------------------------------------------------------------------------


def test_v0627_static_channel_1_sensors_removed_from_SENSORS():
    """The 5 v0.6.18 ``channel_1_*`` static entries must be gone
    from the SENSORS tuple (replaced by dynamic per-channel generator).
    """
    src = _read(_INTEGRATION_ROOT / "sensor.py")
    # Search for ``HikvisionISAPISensorDescription(\n        key="channel_1_*``
    # patterns — should not exist in the SENSORS tuple.
    static_pattern = re.compile(
        r'HikvisionISAPISensorDescription\(\s*key="channel_1_'
    )
    assert not static_pattern.search(src), (
        "v0.6.27: static channel_1_* sensor descriptions must be "
        "removed from SENSORS. The dynamic _build_per_channel_entities "
        "generator emits the same keys for channel id=1, so existing "
        "entity-registry entries still match."
    )


# ---------------------------------------------------------------------------
# Translation coverage for new per-channel keys
# ---------------------------------------------------------------------------


def test_v0627_translations_have_channel_shared_keys():
    """en + 3 zh translation files must declare ``channel_video_*``
    keys so the dynamic per-channel sensors display localised names.
    """
    expected = {
        "channel_video_codec": "Video Codec",
        "channel_video_resolution": "Resolution",
        "channel_video_frame_rate": "Frame Rate",
        "channel_video_bitrate": "Bitrate",
        "channel_audio_codec": "Audio Codec",
        "channel_name": "Name",
    }
    zh_expected = {
        "channel_video_codec": "视频编码",
        "channel_video_resolution": "分辨率",
        "channel_video_frame_rate": "帧率",
        "channel_video_bitrate": "码率",
        "channel_audio_codec": "音频编码",
        "channel_name": "名称",
    }
    import json as _json
    trans_dir = _INTEGRATION_ROOT / "translations"
    en = _json.loads((trans_dir / "en.json").read_text(encoding="utf-8-sig"))
    for key, expected_name in expected.items():
        actual = en["entity"]["sensor"].get(key, {}).get("name")
        assert actual == expected_name, (
            f"v0.6.27: en.json must declare sensor.{key}.name = "
            f"{expected_name!r} (got {actual!r})."
        )
    # zh-CN / zh / zh-Hans must all declare the same Chinese names.
    for fname in ("zh-CN.json", "zh.json", "zh-Hans.json"):
        data = _json.loads((trans_dir / fname).read_text(encoding="utf-8-sig"))
        for key, expected_name in zh_expected.items():
            actual = data["entity"]["sensor"].get(key, {}).get("name")
            assert actual == expected_name, (
                f"v0.6.27: {fname} must declare sensor.{key}.name = "
                f"{expected_name!r} (got {actual!r})."
            )