"""Regression tests for the four bugs fixed in v0.6.22.

User-reported after v0.6.21:

- ``time_mode`` sensor stayed "未知" forever on every device.
  Root cause: ``_fetch_time(client)`` was called AFTER the
  ``async with self._make_client() as client:`` block ended
  (client already closed via ``__aexit__`` → ``aclose()``).
- IPC 通道 1 视频编码 / 分辨率 / 帧率 / 码率 / 音频编码 all
  showed "未知" even though the data is in
  ``/ISAPI/Streaming/channels`` list response.
  Root cause: ``_fetch_streaming`` was hitting
  ``/Streaming/channels/1`` (per-channel config endpoint) which
  returns a single ``<StreamingChannel>`` without the nested
  ``<Video>``/``<Audio>`` blocks the parser reads.
- ``memory_available_mb`` rendered as "402 s" instead of "402 MB".
  Root cause: native_unit_of_measurement was ``UnitOfTime.SECONDS``
  and device_class was ``DATA_SIZE`` (a v0.6.17-era copy-paste bug).
- Storage 4 sensors appeared on IPC as 3-4 always-unknown entities.
  Root cause: storage sensors registered unconditionally.
  Fix: skip them when ``coordinator.device_type`` is IPC.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_COORD_SRC = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance\coordinator.py"
)
_SENSOR_SRC = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance\sensor.py"
)


def _extract_function_body(source: str, def_line: str) -> str | None:
    """Return the body of the first function whose definition starts with
    ``def_line``, or ``None`` if not found.

    The body is everything from the def line up to (but not
    including) the next top-level ``def`` / ``class`` / ``@``.
    """
    idx = source.find(def_line)
    if idx < 0:
        return None
    rest = source[idx + len(def_line):]
    boundary = None
    for marker in ("\ndef ", "\nclass ", "\n@"):
        pos = rest.find(marker, 1)
        if pos > 0 and (boundary is None or pos < boundary):
            boundary = pos
    if boundary is None:
        return rest
    return rest[:boundary]


# ---- bug 1: _fetch_time must be called inside the async with block ----


def test_fetch_time_inside_async_with_block():
    """``_fetch_time(client)`` must be inside the ``async with``
    block where ``client`` is still alive.

    Pre-v0.6.22 the call was after the block ended → ``client`` was
    closed via ``__aexit__`` → ``aclose()`` → the httpx session
    raised errors on every call → ``time_mode`` always "unknown".
    """
    import re

    src = _COORD_SRC.read_text(encoding="utf-8-sig")

    # Find the second `async with self._make_client() as client:`
    # block. The block header has 4-space indent, body has 8-space.
    block_marker = "async with self._make_client() as client:"
    first = src.find(block_marker)
    assert first > 0
    second = src.find(block_marker, first + len(block_marker))
    assert second > 0, (
        "v0.6.14: expected a SECOND `async with self._make_client()` "
        "block for the per-device-type-dependent endpoints."
    )

    # Locate the block header line end (so we start scanning from
    # the first body line).
    block_header_end = src.find("\n", second) + 1
    # Scan forward, line by line, until we find a non-blank,
    # non-comment line with leading indent <= 4 (i.e. it's outside
    # the 8-space-indented block).
    pos = block_header_end
    block_end = len(src)
    while pos < len(src):
        nl = src.find("\n", pos)
        if nl < 0:
            line = src[pos:]
            nl = len(src)
        else:
            line = src[pos:nl]
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            leading = len(line) - len(line.lstrip(" "))
            if leading <= 4:
                block_end = pos
                break
        pos = nl + 1

    block_body = src[block_header_end:block_end]
    assert "_fetch_time(client)" in block_body, (
        "v0.6.22: _fetch_time(client) must be called INSIDE the "
        "second async with self._make_client() as client: block. "
        "Pre-v0.6.22 the call was after the block, by which point "
        "the client was already closed → time_mode always 'unknown'."
        f"\n\n--- block body ---\n{block_body}\n--- end ---"
    )


# ---- bug 2: _fetch_streaming must hit the LIST endpoint ----


def test_fetch_streaming_uses_list_endpoint():
    """``_fetch_streaming`` must fetch ``/ISAPI/Streaming/channels``,
    NOT ``/Streaming/channels/1``.

    Pre-v0.6.22 the per-channel endpoint returned a single
    ``<StreamingChannel>`` without the nested ``<Video>`` /
    ``<Audio>`` blocks the parser reads → every codec /
    resolution / frame-rate / audio sensor showed "unknown" on
    the user's V5 IPC.
    """
    src = _COORD_SRC.read_text(encoding="utf-8-sig")
    # Get the function body, but exclude the docstring (which may
    # mention the buggy endpoint to explain what was changed).
    body = _extract_function_body(src, "async def _fetch_streaming(")
    assert body is not None
    # Strip the docstring so its historical references don't trip the
    # not-in-body checks.
    doc_end = body.find('"""', body.find('"""') + 3)
    if doc_end > 0:
        code = body[doc_end + 3:]
    else:
        code = body

    # Must use ISAPI_STREAMING_CHANNELS (list endpoint) without
    # appending a channel id.
    assert "client.get_xml(ISAPI_STREAMING_CHANNELS)" in code, (
        "v0.6.22: _fetch_streaming must call "
        "``client.get_xml(ISAPI_STREAMING_CHANNELS)`` — the list "
        "endpoint at ``/ISAPI/Streaming/channels``."
    )
    # Forbid the buggy per-channel endpoint shape.
    for bad in (
        'f"{ISAPI_STREAMING_CHANNELS}/1"',
        'f"{ISAPI_STREAMING_CHANNELS}/{id}"',
        "/Streaming/channels/1",
    ):
        assert bad not in code, (
            f"v0.6.22: _fetch_streaming must NOT fetch {bad!r} — "
            "that's the per-channel config endpoint which doesn't "
            "have the nested <Video>/<Audio> blocks."
        )


# ---- bug 3: memory_available_mb must show MB, not seconds ----


def test_memory_available_mb_uses_megabytes_unit():
    """The memory_available_mb sensor must use UnitOfInformation.MEGABYTES,
    not UnitOfTime.SECONDS, and must NOT use DATA_SIZE device class.

    Pre-v0.6.22 this was set to ``UnitOfTime.SECONDS`` with a
    ``DATA_SIZE`` device_class — HA rendered the integer MB value
    as "402 s" on the user's NVR.
    """
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    # Extract just the memory_available_mb HikvisionISAPISensorDescription
    # block (between its key= line and the next HikvisionISAPISensorDescription).
    section = src.split('key="memory_available_mb"', 1)[1]
    next_entry = section.find("\n    HikvisionISAPISensorDescription(")
    if next_entry > 0:
        section = section[:next_entry]

    # Active code: drop the v0.6.22 docstring/comment block so the
    # fix-history reference to ``UnitOfTime.SECONDS`` doesn't trip
    # the not-in-section check. Code starts at the
    # ``native_unit_of_measurement=`` line.
    code_start = section.find("native_unit_of_measurement=")
    if code_start > 0:
        code = section[code_start:]
    else:
        code = section

    assert "UnitOfInformation.MEGABYTES" in code, (
        "v0.6.22: memory_available_mb must set "
        "``native_unit_of_measurement=UnitOfInformation.MEGABYTES``."
    )
    # Specifically forbid the buggy assignment shape.
    assert "native_unit_of_measurement=UnitOfTime.SECONDS" not in code, (
        "v0.6.22: memory_available_mb must NOT have "
        "``native_unit_of_measurement=UnitOfTime.SECONDS`` (the v0.6.17 "
        "copy-paste bug that rendered as '402 s' instead of '402 MB')."
    )
    # Forbid the buggy device_class assignment shape.
    assert "device_class=SensorDeviceClass.DATA_SIZE" not in code, (
        "v0.6.22: memory_available_mb must NOT have "
        "``device_class=SensorDeviceClass.DATA_SIZE`` — the value is "
        "in MB, not bytes; DATA_SIZE makes HA interpret the integer "
        "as bytes."
    )


# ---- bug 4: storage sensors must be skipped on IPC ----


def test_storage_sensors_skipped_on_ipc():
    """``async_setup_entry`` must filter out storage sensors when the
    coordinator's ``device_type`` is IPC (camera). They are useful
    only on NVR/DVR.
    """
    src = _SENSOR_SRC.read_text(encoding="utf-8-sig")
    body = _extract_function_body(src, "async def async_setup_entry(")
    assert body is not None, "async_setup_entry not found in sensor.py"
    # Must check device_type against NVR/DVR and filter storage_*
    # when not a recorder.
    assert "device_type" in body, (
        "v0.6.22: async_setup_entry must inspect coordinator.device_type."
    )
    assert "storage_" in body, (
        "v0.6.22: async_setup_entry must filter out storage_* sensors "
        "on non-recorder device types."
    )
    # Must reference the NVR/DVR device-type constants.
    assert "DEVICE_TYPE_NETWORK_VIDEO_RECORDER" in body or "DEVICE_TYPE_DVR" in body, (
        "v0.6.22: async_setup_entry must gate storage sensor "
        "registration on NVR/DVR device types."
    )


# ---- integration: ensure _parse_streaming_detail handles IPC XML ----


def test_parse_streaming_detail_extracts_ipc_video_fields():
    """_parse_streaming_detail must read codec / resolution / framerate /
    audio from a real IPC ``<StreamingChannelList>`` response.
    """
    import sys

    sys.path.insert(0, str(_COORD_SRC.parent.parent))
    from custom_components.hikvision_isapi_performance import coordinator as coord

    # Real-ish V5 IPC XML. Matches the user's DS-FB2127.
    xml = """<StreamingChannelList>
<StreamingChannel>
<id>1</id>
<channelName>摄像机06</channelName>
<Video>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>1920</videoResolutionWidth>
<videoResolutionHeight>1080</videoResolutionHeight>
<constantBitRate>2048</constantBitRate>
<maxFrameRate>1600</maxFrameRate>
</Video>
<Audio>
<audioCompressionType>G.711alaw</audioCompressionType>
</Audio>
</StreamingChannel>
</StreamingChannelList>"""

    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    detail = coord._parse_streaming_detail(root)

    assert detail["first_channel_id"] == "1"
    assert detail["video_codec"] == "H.264"
    assert detail["video_resolution"] == "1920x1080"
    assert detail["video_resolution_width"] == 1920
    assert detail["video_resolution_height"] == 1080
    # Hikvision reports maxFrameRate in 1/100 fps: 1600 = 16 fps.
    assert detail["video_frame_rate"] == 16.0
    assert detail["video_bitrate_kbps"] == 2048
    assert detail["audio_codec"] == "G.711alaw"
    assert len(detail["channels"]) == 1


# ---- integration: ensure _parse_time handles NVR + IPC XML ----


def test_parse_time_extracts_time_mode_from_v4_nvr():
    """_parse_time must read timeMode from a real V4 NVR ``<Time>``
    response (lowercase tag)."""
    import sys

    sys.path.insert(0, str(_COORD_SRC.parent.parent))
    from custom_components.hikvision_isapi_performance import coordinator as coord

    import xml.etree.ElementTree as ET
    xml = """<Time>
<timeMode>manual</timeMode>
<localTime>2026-09-25T08:27:27+08:00</localTime>
<timeZone>CST-8:00:00</timeZone>
</Time>"""
    root = ET.fromstring(xml)
    info = coord._parse_time(root)
    assert info["time_mode"] == "manual"
    assert info["local_time"] == "2026-09-25T08:27:27+08:00"
    assert info["time_zone"] == "CST-8:00:00"


def test_parse_time_extracts_time_mode_from_v5_ipc():
    """_parse_time must read timeMode from a real V5 IPC ``<Time>``
    response."""
    import sys

    sys.path.insert(0, str(_COORD_SRC.parent.parent))
    from custom_components.hikvision_isapi_performance import coordinator as coord

    import xml.etree.ElementTree as ET
    xml = """<Time>
<timeMode>NTP</timeMode>
<localTime>2026-09-25T08:36:01+08:00</localTime>
<timeZone>CST-8:00:00</timeZone>
</Time>"""
    root = ET.fromstring(xml)
    info = coord._parse_time(root)
    assert info["time_mode"] == "NTP"