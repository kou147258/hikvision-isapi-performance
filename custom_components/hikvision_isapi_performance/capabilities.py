"""Entity-level capability gating for Hikvision ISAPI (v0.8).

Answers one question: **does this device actually provide the data an
entity would show?** Platforms consult this module so they only create
entities that have a real data source — eliminating the permanently
"unknown" entities the user reported (reboot count on NVRs, recording
state, motion config on devices that 403 the endpoint).

This is deliberately separate from ``coordinator._parse_capabilities``,
which parses ``/System/capabilities`` for *device classification*
(IPC/NVR/DVR cross-check, channel-count validation). This module gates
*individual entities* across many endpoints.

Every function is pure — XML/dict in, structured result out, no network
I/O — so the whole surface is covered by tests driven with real device
captures (``tests/fixtures_v08``).

Fleet facts this encodes (12 devices probed 2026-09-27):
  * ``totalRebootCount`` — 6/12 return it; 3 IPCs + all 3 NVRs lack it.
    A value of 0 is distinct from an absent field.
  * ``InputProxyChannelList`` ``size`` attribute is **unreliable**:
    176.64 declares size=19 (13 real), 176.65 declares size=0 (8 real).
    Always count ``<InputProxyChannel>`` blocks.
  * trackID = ``{channel}01`` (101, 201, … 1301). Out-of-range trackIDs
    cause a whole-request 400 on some devices, so generate exactly the
    channels present.
  * ``searchResultPosition=0`` returns the *earliest* segments, so the
    search window must stay short or stale data is returned.
  * ``maxResults`` truncates (``responseStatusStrg=MORE``) below
    ``2 × channels`` — 13 tracks with maxResults=13 covered only 12.
  * motionDetection — 11/12 usable (176.65 → 403). ``sensitivityLevel``
    may be nested under ``MotionDetectionLayout`` or flat.
  * recording search — endpoint usable on 9/12, real data on 4/12.

⚠️ **``/Event/triggers`` must NOT gate event sensors.** The catalog it
returns is not what the device actually pushes. Probed on the fleet:
176.18, 176.12, 176.13 and 176.52 all list ``VMD`` + ``tamperdetection``
but **none lists ``videoloss``** — while alertStream delivers
``videoloss`` events on 10 of 11 devices. Gating on the catalog would
therefore never create a video_loss sensor on any device. ``parse_event_types``
is a *diagnostic* only (which smart features the firmware advertises);
event sensors are created from what alertStream is observed to emit.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.etree import ElementTree as ET

__all__ = [
    "SEARCH_WINDOW_MINUTES",
    "has_reboot_count",
    "reboot_count_value",
    "count_proxy_channels",
    "track_ids",
    "search_max_results",
    "parse_motion_detection",
    "build_motion_detection_body",
    "MOTION_SENSITIVITY_MIN",
    "MOTION_SENSITIVITY_MAX",
    "parse_event_types",
    "event_types_to_sensor_keys",
    "recording_available",
    "parse_recording_segments",
    "derive_recording_status",
    "DEFAULT_RECORDING_TOLERANCE_SECONDS",
    "build_search_body",
    "parse_streaming_sessions",
    # v0.9 batch 1: fields already present in the /System/status response
    # the integration fetches on every poll, but which the pre-v0.9 parser
    # discarded. Zero extra network cost.
    "parse_dome_info",
    "parse_camera_usage",
    "parse_status_extras",
]

# Search window for recording-segment queries. Kept short on purpose:
# ``searchResultPosition=0`` returns the *earliest* matching segments, so
# a long window (probed: 24 h) returns stale data from the start of the
# window and hides the current recording position. 5 minutes reliably
# surfaces the segment being written right now without ever tripping the
# whole-request 400 seen with larger window+maxResults combinations.
SEARCH_WINDOW_MINUTES = 5

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})

# alertStream / Event-triggers eventType → binary_sensor key we build.
# Both the trigger-catalog spelling (``tamperdetection``) and the
# alertStream spelling (``tamper``) map to the same key. Anything not
# listed produces no sensor — we never create an entity for an event
# type we don't model.
_EVENT_TO_SENSOR_KEY = {
    "vmd": "motion",
    "motion": "motion",
    "videoloss": "video_loss",
    "video_loss": "video_loss",
    "tamper": "tamper",
    "tamperdetection": "tamper",
}

# ``<eventType>facedetection-1</eventType>`` → base type ``facedetection``.
_SUFFIX_RE = re.compile(r"-\d+$")

# Hikvision documents carry a default namespace. Stripping it lets plain
# tag names match in ``find``/``findall`` — the same trick
# ``isapi_client._strip_xmlns`` applies to fetched responses. Kept local
# so this module stays free of client imports (it is pure parsing).
_XMLNS_RE = re.compile(r'\s+xmlns(?::\w+)?\s*=\s*["\'][^"\']*["\']')


def _strip_xmlns(text: str) -> str:
    return _XMLNS_RE.sub("", text)


def _text(el: ET.Element | None, *path: str) -> str | None:
    if el is None:
        return None
    node = el
    for tag in path:
        node = node.find(tag)
        if node is None:
            return None
    return node.text


def _as_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    s = raw.strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def _as_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


def _parse_dt(raw: str | None) -> datetime | None:
    """Parse an ISAPI timestamp to a tz-aware datetime.

    Accepts both ``...Z`` and ``...+08:00`` forms (the fleet shows both
    across endpoints). Returns None on anything unparseable so callers
    show "unknown" rather than a wrong date.
    """
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── 1. reboot_count gating ─────────────────────────────────────────


def has_reboot_count(root: ET.Element | None) -> bool:
    """True only when ``<totalRebootCount>`` is actually present.

    A reported ``0`` counts as present (the device did answer, "rebooted
    0 times" is real data). An absent field returns False so the entity
    is not created — otherwise the six NVRs/older IPCs would show a
    permanent "unknown".
    """
    if root is None:
        return False
    node = root.find(".//totalRebootCount")
    return node is not None and node.text is not None


def reboot_count_value(root: ET.Element | None) -> int | None:
    """The integer reboot count, or None when absent/non-numeric."""
    if root is None:
        return None
    return _as_int(_text(root, ".//totalRebootCount"))


# ── 2. channel count (size attribute is a trap) ────────────────────


def count_proxy_channels(root: ET.Element | None) -> int:
    """Count ``<InputProxyChannel>`` blocks — never trust ``size``.

    Real firmware disagrees with its own ``size`` attribute:
    176.64 says size=19 but lists 13 channels; 176.65 says size=0 but
    lists 8. ``findall`` matches by tag name regardless of the optional
    ``version`` attribute some firmwares add to the open tag.
    """
    if root is None:
        return 0
    return len(root.findall(".//InputProxyChannel"))


# ── 3. recording-search parameters ─────────────────────────────────


def track_ids(channel_count: int) -> list[str]:
    """Build the trackID list for a recording search.

    Convention: ``{channel}01`` (main stream). Probed on 176.64:
    trackIDs 101, 201, … 1301 all return that channel's recordings.
    Generating more trackIDs than the device has channels makes some
    firmwares reject the *whole* request with 400 "Invalid track id",
    so callers must pass the exact count.
    """
    if channel_count <= 0:
        return []
    return [f"{i}01" for i in range(1, channel_count + 1)]


def search_max_results(channel_count: int) -> int:
    """``maxResults`` large enough that no channel is truncated away.

    The device caps returned segments at ``maxResults`` and flags
    ``responseStatusStrg=MORE`` when there are more. Probed: 13 tracks
    with maxResults=13 returned only 12 distinct trackIDs (one channel
    lost to truncation); maxResults=20 returned all 13. Two per channel
    is the safe margin (a channel being written spans the previous and
    current segment), with a floor of 10 for single-channel devices.
    """
    return max(2 * channel_count, 10)


# ── 4. motion detection config ─────────────────────────────────────


def parse_motion_detection(root: ET.Element | None) -> dict[str, Any] | None:
    """Extract ``enabled`` + ``sensitivity_level`` from motionDetection.

    Returns None when the endpoint failed (root is None — e.g. 176.65's
    403), so no switch/number entity is created there. When the endpoint
    answered, returns a dict whose fields may individually be None (the
    device may omit one) — never a fabricated default.

    ``sensitivityLevel`` appears nested under ``MotionDetectionLayout``
    on the fleet's firmware but some variants put it flat on the root;
    both are read.
    """
    if root is None:
        return None
    enabled = _as_bool(_text(root, "enabled"))
    sens = _as_int(_text(root, ".//sensitivityLevel"))
    return {"enabled": enabled, "sensitivity_level": sens}


# Sensitivity domain is 0..100 on every fleet device (``sensitivityLevel``).
MOTION_SENSITIVITY_MIN = 0
MOTION_SENSITIVITY_MAX = 100


def build_motion_detection_body(
    raw_xml: str,
    *,
    enabled: bool | None = None,
    sensitivity_level: int | None = None,
) -> str:
    """Build a motionDetection PUT body that changes ONLY what was asked.

    This is the one place the integration writes camera *configuration*, so
    it must not be destructive. The real document carries the user's
    detection region and timings:

        <regionType>grid</regionType>
        <Grid><rowGranularity>18</rowGranularity>
              <columnGranularity>22</columnGranularity></Grid>
        <samplingInterval>5</samplingInterval>
        <startTriggerTime>1000</startTriggerTime><endTriggerTime>1000</endTriggerTime>
        <MotionDetectionLayout><sensitivityLevel>60</sensitivityLevel>
          <layout><gridMap>fffffc...</gridMap></layout></MotionDetectionLayout>

    Sending a hand-built ``<MotionDetection><enabled>false</enabled>
    </MotionDetection>`` would reset every field we omitted — wiping the
    gridMap the user drew and forcing them to re-mask the scene. So we
    parse the captured document, mutate only the targeted nodes, and
    serialize it back with everything else byte-identical.

    ``enabled`` and ``sensitivity_level`` may both be given (the caller is
    writing a value it just read back) but at least one is required —
    passing neither is a caller bug, and silently PUTting an unchanged
    document would hide it.
    """
    if enabled is None and sensitivity_level is None:
        raise ValueError(
            "build_motion_detection_body needs enabled and/or sensitivity_level"
        )
    if sensitivity_level is not None and not (
        MOTION_SENSITIVITY_MIN <= sensitivity_level <= MOTION_SENSITIVITY_MAX
    ):
        raise ValueError(
            f"sensitivity_level {sensitivity_level} outside "
            f"{MOTION_SENSITIVITY_MIN}..{MOTION_SENSITIVITY_MAX}"
        )

    root = None
    if raw_xml and raw_xml.strip():
        try:
            root = ET.fromstring(_strip_xmlns(raw_xml).strip())
        except ET.ParseError:
            root = None

    if root is None:
        # No usable captured document (probe failed to retain raw XML).
        # Emit a minimal document with only the requested field rather
        # than guessing the device's defaults.
        root = ET.Element("MotionDetection")

    if enabled is not None:
        node = root.find("enabled")
        if node is None:
            # Insert as the first child: firmware is order-tolerant on
            # read but the canonical document has <enabled> first.
            node = ET.Element("enabled")
            root.insert(0, node)
        node.text = "true" if enabled else "false"

    if sensitivity_level is not None:
        node = root.find(".//sensitivityLevel")
        if node is None:
            layout = root.find("MotionDetectionLayout")
            if layout is None:
                layout = ET.SubElement(root, "MotionDetectionLayout")
            node = ET.SubElement(layout, "sensitivityLevel")
        node.text = str(sensitivity_level)

    return ET.tostring(root, encoding="unicode")


# ── 5. event-type catalog ──────────────────────────────────────────


def parse_event_types(root: ET.Element | None) -> set[str]:
    """Collect supported event types from ``/Event/triggers``.

    Strips per-channel numeric suffixes (``facedetection-1`` →
    ``facedetection``) so the set describes *which kinds* of events the
    device can emit, not how many channels. Returns an empty set when
    the endpoint is unavailable (3/12 devices) — callers then fall back
    to the event types actually seen on alertStream.
    """
    if root is None:
        return set()
    out: set[str] = set()
    for node in root.findall(".//eventType"):
        raw = (node.text or "").strip()
        if not raw:
            continue
        out.add(_SUFFIX_RE.sub("", raw))
    return out


def event_types_to_sensor_keys(event_types: set[str]) -> set[str]:
    """Map device event types to the binary-sensor keys we build.

    Only VMD (motion), videoloss (video_loss) and tamper are modelled;
    everything else (diskfull, ipconflict, …) yields no sensor. This is
    what keeps a device that advertises 18 event types from sprouting
    18 entities, 15 of which we can't render meaningfully.
    """
    keys: set[str] = set()
    for et in event_types or ():
        key = _EVENT_TO_SENSOR_KEY.get((et or "").strip().lower())
        if key:
            keys.add(key)
    return keys


# ── 6. recording-search results ────────────────────────────────────


def recording_available(root: ET.Element | None) -> bool:
    """True when the search returned at least one recording segment.

    ``NO MATCHES`` (device has no storage / recording not configured)
    and a failed endpoint (root is None — 176.51/52/53 return 403) both
    yield False, so no recording entity is created. This is the gate
    that replaces the old always-"unknown" recording binary sensor.
    """
    if root is None:
        return False
    return bool(root.findall(".//searchMatchItem"))


def parse_recording_segments(root: ET.Element | None) -> list[dict[str, Any]]:
    """Parse each ``<searchMatchItem>`` into a structured segment.

    Fields per segment: ``track_id``, ``start``/``end`` (tz-aware
    datetimes), ``codec_type``, ``lock_status``, ``record_type``. The
    caller derives "recording active" (latest end >= now - tolerance)
    and "last recording time" from these — see the v0.8 design doc for
    why the end time is a *planned* segment boundary, not the live
    write position.
    """
    if root is None:
        return []
    segments: list[dict[str, Any]] = []
    for item in root.findall(".//searchMatchItem"):
        track = _text(item, "trackID")
        start = _parse_dt(_text(item, ".//startTime"))
        end = _parse_dt(_text(item, ".//endTime"))
        if track is None or start is None or end is None:
            continue
        codec = _text(item, ".//codecType")
        lock = _text(item, ".//lockStatus")
        # metadataDescriptor looks like
        # "recordType.meta.hikvision.com/timing" → record_type "timing".
        meta = _text(item, ".//metadataDescriptor") or ""
        record_type = meta.rsplit("/", 1)[-1] if meta else None
        segments.append({
            "track_id": track.strip(),
            "start": start,
            "end": end,
            "codec_type": (codec or "").strip() or None,
            "lock_status": (lock or "").strip() or None,
            "record_type": record_type,
        })
    return segments


# ── 7. recording-activity derivation ───────────────────────────────

# Default tolerance for "is this channel recording right now?".
#
# The device pre-allocates each segment's *planned* end time, so the
# newest segment's ``end`` normally sits at or slightly after "now".
# Probed on 176.10: a fresh segment's end was 3 seconds *before* the
# sample instant (the next segment had not yet been written to the
# index), which made a strict ``end >= now`` test flip to False at every
# segment boundary. 60 s absorbs that boundary jitter without being so
# loose that a genuinely-stopped channel looks active for long.
DEFAULT_RECORDING_TOLERANCE_SECONDS = 60


def _channel_from_track(track_id: str) -> str | None:
    """``101`` → ``"1"``, ``1301`` → ``"13"``; None if not derivable.

    trackID is ``{channel}{stream}`` with a 2-digit stream suffix, so the
    channel number is everything but the last two characters. A trackID
    that doesn't fit that shape (too short, non-numeric) is skipped
    rather than guessed.
    """
    t = (track_id or "").strip()
    if len(t) < 3 or not t.isdigit():
        return None
    return t[:-2]


def derive_recording_status(
    segments: list[dict[str, Any]],
    now: datetime | None = None,
    tolerance_seconds: int = DEFAULT_RECORDING_TOLERANCE_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Map recording segments to per-channel recording activity.

    Returns ``{channel_id: {recording_active, last_recording_time,
    codec_type, record_type}}``. Empty dict when there are no segments
    (NO MATCHES, or an endpoint that 403'd) so no recording entity is
    created.

    **This is a derived value, not a device-reported status bit.** The
    device gives us segment *planned* end times, not a live
    "recording=true" flag (``Recording/channels/*/status`` is 403/404 on
    all 12 fleet devices). The rule is:

        recording_active = (newest segment end >= now - tolerance)

    Consequences the user must know (documented in v0.8-design.md):
      * A channel that just stopped recording stays ``True`` until its
        last pre-allocated segment's planned end passes — detection lag
        ≈ one segment length (17–358 min observed on the fleet).
      * ``last_recording_time`` is always the newest segment end, so the
        user can see how stale the "active" verdict is.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Newest end per channel, plus that segment's codec/recordType.
    latest: dict[str, dict[str, Any]] = {}
    for seg in segments:
        end = seg.get("end")
        if end is None:
            continue
        ch = _channel_from_track(seg.get("track_id", ""))
        if ch is None:
            continue
        cur = latest.get(ch)
        if cur is None or end > cur["end"]:
            latest[ch] = {
                "end": end,
                "codec_type": seg.get("codec_type"),
                "record_type": seg.get("record_type"),
            }

    out: dict[str, dict[str, Any]] = {}
    cutoff = now - timedelta(seconds=tolerance_seconds)
    for ch, info in latest.items():
        out[ch] = {
            "recording_active": info["end"] >= cutoff,
            "last_recording_time": info["end"],
            "codec_type": info["codec_type"],
            "record_type": info["record_type"],
        }
    return out


# ── 8. recording-search request body ───────────────────────────────


def build_search_body(
    track_ids: list[str],
    window_minutes: int = SEARCH_WINDOW_MINUTES,
    max_results: int = 20,
    now: datetime | None = None,
    search_id: str | None = None,
) -> str:
    """Build the ``POST /ISAPI/ContentMgmt/search`` request body.

    The exact shape was validated against the fleet (returns 200 on all
    devices that support the endpoint). Notes encoded from probing:

      * ``searchResultPosition=0`` returns the *earliest* segments in the
        window, so ``window_minutes`` must stay short (default 5) — a long
        window returns stale data and hides the current recording position.
      * ``max_results`` must be >= 2 × channel count (see
        ``search_max_results``); too small truncates channels away.
      * ``searchID`` must be unique per query — the device deduplicates
        and paginates on it. Callers omit it in production (a UUID is
        generated); tests inject a fixed one.
      * ``now`` is injectable so tests are deterministic; naive datetimes
        are treated as UTC.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    start = (now - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    sid = search_id if search_id is not None else str(uuid.uuid4()).upper()
    ids = "".join(f"<trackID>{t}</trackID>" for t in track_ids)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<CMSearchDescription>\n"
        f"<searchID>{sid}</searchID>\n"
        f"<trackIDList>{ids}</trackIDList>\n"
        "<timeSpanList><timeSpan>"
        f"<startTime>{start}</startTime><endTime>{end}</endTime>"
        "</timeSpan></timeSpanList>\n"
        f"<maxResults>{max_results}</maxResults>\n"
        "<searchResultPosition>0</searchResultPosition>\n"
        "<metadataList><metadataDescriptor>"
        "//recordType.meta.std-cgi.com"
        "</metadataDescriptor></metadataList>\n"
        "</CMSearchDescription>"
    )


# ── 8. active streaming sessions ──────────────────────────────────


def parse_streaming_sessions(root: ET.Element | None) -> int | None:
    """Count active streaming sessions, or None if this isn't a session list.

    Discovered while fixing the v0.8 ``online`` tri-state bug: the IPC
    per-channel status endpoint ``/ISAPI/Streaming/channels/{id}/status``
    does **not** return a channel status. It returns a
    ``StreamingSessionStatusList`` — the set of currently-active streaming
    sessions. Probed across the 12-device fleet (probe_sessions.py,
    2026-10-04):

    ==================  ==================================  ========
    Device              status endpoint response            sessions
    ==================  ==================================  ========
    9 IPCs              ``StreamingSessionStatusList``      2–7
    176.64 NVR          ``InputProxyChannelStatus``         —
    176.65 DVR          ``InputProxyChannelStatus``         —
    192.168.10.17 NVR   ``InputProxyChannelStatus``         —
    ==================  ==================================  ========

    The device-level ``/ISAPI/Streaming/sessions`` endpoint fails on
    **12/12** devices, so this per-channel response is the only source.

    Returns:
      * ``int`` — session count, when the response really is a session
        list. **``0`` is a valid, meaningful reading** (nobody is pulling
        the stream right now) and must not be conflated with ``None``.
      * ``None`` — any other response shape (the NVR/DVR
        ``InputProxyChannelStatus``, or a failed endpoint). Callers use
        this to skip entity creation entirely, rather than building a
        permanently-empty sensor on devices that never report sessions.

    Privacy: the only payload inside each ``<StreamingSessionStatus>`` is
    ``clientAddress/ipAddress`` — probed, there is no sessionId, stream
    type, or transport info. So this function deliberately returns a bare
    count and **never** the addresses: exposing them would publish the
    user's LAN topology (who is watching which camera) for no monitoring
    benefit. See test_session_parser_never_exposes_client_ips.
    """
    if root is None:
        return None
    tag = root.tag.split("}")[-1]  # strip any XML namespace
    if tag != "StreamingSessionStatusList":
        return None
    return len(root.findall(".//StreamingSessionStatus"))


# ── 9. v0.9: /System/status fields the pre-v0.9 parser discarded ──
#
# ``/ISAPI/System/status`` is fetched on EVERY poll and already parsed by
# ``coordinator._parse_system_status`` — but that parser only extracted 8
# fields and threw the rest of the response away. Probing the full
# response across the 12-device fleet (probe_captures/v09_wide, captured
# 2026-10-04) showed three more groups of real telemetry sitting in the
# same payload, at ZERO extra network cost:
#
#   DomeInfoList  PTZ/dome lifetime counters   6/12 devices (dome IPCs only)
#   CameraList    lens actuation counters      6/12 devices (same six)
#   memoryDescription / cpuDescription         12/12 and 11/12
#   batteryAllowance / videoRewritingTimes     1/12 (176.10 only)
#
# Fleet split (real captures):
#   dome + camera present : 176.10, 176.12, 176.13, 176.51, 176.52, 176.53
#   absent entirely       : 176.16, 176.17, 176.18 (fixed-lens IPCs),
#                           176.64, 176.65, R17.17 (recorders)
#
# Unit proof (not an assumption): on all six dome devices the three
# temperature-bucket runtimes sum EXACTLY to domeRunTotalTime, e.g.
# 176.10: 0 + 248171 + 360134 = 608305. cameraRunTotalTime equals
# domeRunTotalTime on the same six, and both are the same order of
# magnitude as deviceUpTime (762399 s) — so these are seconds.


def _block(root: ET.Element | None, tag: str) -> ET.Element | None:
    """Return the first descendant element named ``tag``, or None."""
    if root is None:
        return None
    return root.find(f".//{tag}")


# DomeInfo XML tag → result key. Kept as a table so the mapping is
# reviewable in one place and so adding a field cannot silently drift.
_DOME_FIELDS: dict[str, str] = {
    "domeRunTotalTime": "dome_run_total_time",
    "panTotalRounds": "pan_total_rounds",
    "tiltTotalRounds": "tilt_total_rounds",
    "heatState": "heat_state",
    "fanState": "fan_state",
    "runTimeUnderNegativetwenty": "run_time_under_neg20",
    "runTimeBetweenNtwentyPforty": "run_time_between_neg20_pos40",
    "runtimeOverPositiveforty": "run_time_over_pos40",
    "panFrecRecord": "pan_freq_record",   # firmware typo "Frec" is real
    "tiltFrecRecord": "tilt_freq_record",
}

# CameraList XML tag → result key.
_CAMERA_FIELDS: dict[str, str] = {
    "cameraRunTotalTime": "camera_run_total_time",
    "zoomTotalSteps": "zoom_total_steps",
    "focusTotalSteps": "focus_total_steps",
    "irisTotalSteps": "iris_total_steps",
    "icrTotalSteps": "icr_total_steps",
    "zoomReverseTimes": "zoom_reverse_times",
    "focusReverseTimes": "focus_reverse_times",
    "irisShiftTimes": "iris_shift_times",
    "icrShiftTimes": "icr_shift_times",
    "lensIntirTimes": "lens_init_times",  # firmware typo "Intir" is real
}


def parse_dome_info(root: ET.Element | None) -> dict[str, int | None] | None:
    """Parse ``<DomeInfo>`` (PTZ/dome lifetime counters), or None if absent.

    Returns:
      * ``dict`` — when the device reports a ``<DomeInfo>`` block.
        **A value of ``0`` is real data, not absence**: 176.13/51/52/53
        report every counter as 0 because the dome has never moved.
        Conflating 0 with missing would render those four as "unknown" —
        exactly the symptom the user reported.
      * ``None`` — no ``<DomeInfo>`` block (fixed-lens IPCs, all recorders),
        or a failed endpoint. Callers skip entity creation entirely.
    """
    block = _block(root, "DomeInfo")
    if block is None:
        return None
    out: dict[str, int | None] = {}
    for tag, key in _DOME_FIELDS.items():
        out[key] = _as_int(_text(block, tag))
    return out


def parse_camera_usage(root: ET.Element | None) -> dict[str, int | None] | None:
    """Parse ``<Camera>`` (lens actuation counters), or None if absent.

    Same tri-state contract as :func:`parse_dome_info`: ``0`` is a real
    reading (a brand-new lens that has never zoomed), absence is ``None``.

    These counters are the closest thing Hikvision exposes to lens wear —
    zoom/focus/iris/ICR actuation totals. Useful for spotting a camera
    whose autofocus is hunting (high focus_reverse_times).
    """
    block = _block(root, "Camera")
    if block is None:
        return None
    out: dict[str, int | None] = {}
    for tag, key in _CAMERA_FIELDS.items():
        out[key] = _as_int(_text(block, tag))
    return out


def parse_status_extras(root: ET.Element | None) -> dict[str, Any]:
    """Parse the remaining ``/System/status`` fields worth exposing.

    Always returns a dict (never None) so callers can use ``.get()``
    unconditionally; individual keys are absent/None when the device
    doesn't report them.

    Fleet coverage (12 devices):
      ``memory_description``     12/12  "DDR Memory"
      ``cpu_description``        11/12  R17.17 has no ``<CPUList>``
      ``battery_allowance``       1/12  176.10 only — value 0 is real
      ``video_rewriting_times``   1/12  176.10 only (SD card rewrite count)

    Note ``batteryAllowance=0`` on 176.10 is a genuine reading (this model
    has no battery), distinct from the other 11 devices where the field is
    simply absent.
    """
    out: dict[str, Any] = {
        "memory_description": None,
        "cpu_description": None,
        "battery_allowance": None,
        "video_rewriting_times": None,
    }
    if root is None:
        return out

    mem = _text(root, ".//memoryDescription")
    if mem is not None:
        mem = mem.strip() or None
    cpu = _text(root, ".//cpuDescription")
    if cpu is not None:
        cpu = cpu.strip() or None

    out["memory_description"] = mem
    out["cpu_description"] = cpu
    out["battery_allowance"] = _as_int(_text(root, ".//batteryAllowance"))
    out["video_rewriting_times"] = _as_int(_text(root, ".//videoRewritingTimes"))
    return out
