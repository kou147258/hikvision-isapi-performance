"""alertStream multipart parser + bounded stream reader (v0.8).

Hikvision's ``/ISAPI/Event/notification/alertStream`` is a long-lived
HTTP response carrying ``multipart/mixed`` frames, each an
``<EventNotificationAlert>`` XML document. Consuming it gives sub-second
motion / video-loss / tamper sensors instead of the 30 s poll latency.

Probed on the 12-device fleet (2026-09-27):
  * 12/12 devices serve it (176.18 only over Basic auth).
  * Frames are ``--boundary``-delimited. NVRs 176.65 / 192.168.10.17 emit
    ``channelID=0`` (device-level, not per-channel) videoloss events.
  * **Fire-hose risk**: 176.10 produced 2,252,800 bytes in an 8 s sample
    (≈281 KB/s), ≈78× the next-highest device (176.64: 28,672 bytes).
    An unbounded buffer would exhaust memory, so the parser enforces a
    byte cap and drops the OLDEST bytes on overflow (counted in
    ``dropped_bytes`` for diagnostics, never silent).
  * That same 176.10 sample carried only **10** XML events among **25**
    frames — the rest are non-XML (binary/JPEG) frames. Such frames are
    skipped by the ``EventNotificationAlert`` tag check, so the byte cap
    rather than event volume is the binding constraint. Namespace also
    varies by firmware (``hikvision.com`` vs ``isapi.org``); both are
    stripped before parsing.

Design:
  * ``MultipartEventParser`` is a pure, incremental byte→event parser.
    ``feed(chunk)`` may be called with any split (even one byte at a
    time); a half-frame stays buffered until its delimiter arrives.
    No network, no I/O — fully unit-testable with real captures.
  * De-duplication lives in ``AlertStreamReader._dispatch``, keyed on
    ``(channel_id, event_type)`` last-state so only *consecutive* repeats
    are suppressed. A global "seen set" was tried and removed: it swallows
    the third event of an active→inactive→active sequence, which would
    leave a motion sensor off forever after recovery.
  * ``AlertStreamReader`` owns the httpx streaming connection, reconnect
    backoff, and cancellation. It is deliberately thin and network-bound;
    the parsing logic it depends on is pure.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from .const import ISAPI_EVENT_ALERT_STREAM

_LOGGER = logging.getLogger(__name__)

# Hikvision's alertStream uses the literal token ``boundary`` in its
# multipart delimiter across the whole fleet. Overridable per-instance in
# case a firmware variant differs.
DEFAULT_DELIMITER = b"--boundary"

# Extracts a complete <EventNotificationAlert>...</EventNotificationAlert>
# element from a frame, ignoring the MIME headers that precede it. Tag-based
# (not separator-based) so it survives newline translation and firmware
# variants that separate headers from body inconsistently.
_ALERT_RE = re.compile(
    r"<EventNotificationAlert\b.*?</EventNotificationAlert>",
    re.DOTALL,
)


@dataclass(frozen=True)
class Event:
    """One parsed ``<EventNotificationAlert>``.

    ``channel_id`` of ``"0"`` means a device-level event (observed on
    NVRs 176.65 / 192.168.10.17), not a camera channel.
    """

    channel_id: str
    event_type: str
    event_state: str
    channel_name: str | None = None
    dyn_channel_id: str | None = None
    date_time: str | None = None
    active_post_count: int | None = None


def _strip_xmlns(text: str) -> str:
    return re.sub(r'\s+xmlns(?::\w+)?\s*=\s*["\'][^"\']*["\']', "", text)


def _text(el: ET.Element | None, tag: str) -> str | None:
    if el is None:
        return None
    node = el.find(tag)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _to_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class MultipartEventParser:
    """Incremental, bounded multipart/mixed parser.

    ``feed(chunk)`` returns the list of complete ``Event`` objects that
    became parseable with this chunk. An incomplete trailing frame is
    retained until its delimiter arrives.

    Bounds (both optional, ``None``/0 = unlimited):
      * ``max_buffer_bytes`` — hard cap on retained bytes; on overflow the
        OLDEST bytes are dropped and counted in ``dropped_bytes``.
      * ``max_feed_bytes`` — cap on a single ``feed`` call; an oversized
        chunk keeps only its newest ``max_feed_bytes`` (dropping the head)
        so one giant read can't stall the loop or balloon memory.
    """

    def __init__(
        self,
        *,
        delimiter: bytes = DEFAULT_DELIMITER,
        max_buffer_bytes: int | None = 1_048_576,
        max_feed_bytes: int | None = None,
    ) -> None:
        self._delimiter = delimiter
        self._max_buffer = max_buffer_bytes or 0
        self._max_feed = max_feed_bytes or 0
        self._buffer = b""
        self.dropped_bytes = 0

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[Event]:
        if not chunk:
            return []

        # Per-feed cap: keep the newest slice, drop (and count) the head.
        if self._max_feed and len(chunk) > self._max_feed:
            self.dropped_bytes += len(chunk) - self._max_feed
            chunk = chunk[-self._max_feed:]

        self._buffer += chunk

        # Buffer cap: drop the oldest bytes on overflow.
        if self._max_buffer and len(self._buffer) > self._max_buffer:
            overflow = len(self._buffer) - self._max_buffer
            self.dropped_bytes += overflow
            self._buffer = self._buffer[overflow:]

        # Every segment before the last is a complete frame; the last is
        # an incomplete tail (its closing delimiter hasn't arrived).
        parts = self._buffer.split(self._delimiter)
        self._buffer = parts[-1]

        events: list[Event] = []
        for part in parts[:-1]:
            ev = self._parse_frame(part)
            if ev is not None:
                events.append(ev)
        return events

    def _parse_frame(self, part: bytes) -> Event | None:
        """Parse one inter-delimiter segment into an Event, or None.

        The XML body is located by its tag rather than by splitting on a
        header/body separator. Reason: frame headers are not reliably
        separated by a single blank line across firmwares (and newline
        translation in tooling can turn one ``\\r\\n`` into two ``\\n``),
        so a separator-based split can leave header text glued to the XML
        and break parsing. Tag-based extraction is immune to both.

        Skips non-XML frames (JPEG keepalives), malformed XML, and frames
        lacking an ``eventType`` (nothing to map to a sensor).
        """
        if b"EventNotificationAlert" not in part:
            return None
        text = part.decode("utf-8", "replace")
        m = _ALERT_RE.search(text)
        if m is None:
            # A truncated trailing frame has the opening tag but no close;
            # nothing parseable yet.
            return None
        try:
            root = ET.fromstring(_strip_xmlns(m.group(0)).strip())
        except ET.ParseError:
            return None

        event_type = _text(root, "eventType")
        if not event_type:
            return None
        event_state = _text(root, "eventState") or "active"
        channel_id = _text(root, "channelID")
        if channel_id is None:
            channel_id = _text(root, "dynChannelID") or ""
        return Event(
            channel_id=channel_id,
            event_type=event_type,
            event_state=event_state,
            channel_name=_text(root, "channelName"),
            dyn_channel_id=_text(root, "dynChannelID"),
            date_time=_text(root, "dateTime"),
            active_post_count=_to_int(_text(root, "activePostCount")),
        )


class AlertStreamReader:
    """Owns the alertStream connection: reconnect, backoff, cancellation.

    The parser above is pure; this class is the only network-bound part.
    It is deliberately thin so the interesting logic stays unit-testable
    without a device.

    Lifecycle: ``run()`` loops until ``stopped``; ``stop(task)`` cancels
    the task and closes the client so unloading the integration leaves no
    orphaned connection (HA would otherwise keep the task alive forever).

    **Reconnection matters.** Community reports on the stock Hikvision
    integration describe an event stream that dies permanently on any
    network hiccup until HA restarts. Here every failure path — stream
    ended, timeout, 401/403, connection error, unexpected exception —
    schedules a reconnect with exponential backoff (``reconnect_initial``
    doubling up to ``reconnect_max``). Backoff resets to the initial value
    once events flow again, so a long-lived healthy connection never
    accrues a multi-minute retry delay.

    Dispatch is de-duplicated by **last state per (channel, eventType)**,
    not by a global "seen" set. The difference matters: a global set would
    swallow the third event of an active → inactive → active sequence and
    the motion sensor would stay off forever after recovery. Only
    consecutive repeats are suppressed.

    ``CancelledError`` is never caught (it derives from ``BaseException``),
    so cancellation propagates and the task actually terminates.
    """

    def __init__(
        self,
        client_factory,
        on_event,
        *,
        path: str = ISAPI_EVENT_ALERT_STREAM,
        reconnect_initial: float = 5.0,
        reconnect_max: float = 300.0,
        max_buffer_bytes: int | None = 1_048_576,
        max_feed_bytes: int | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._on_event = on_event
        self._path = path
        self.reconnect_initial = reconnect_initial
        self.reconnect_max = reconnect_max
        self.current_backoff = reconnect_initial
        self.backoff_history: list[float] = []
        self.stopped = False
        self.parser = MultipartEventParser(
            max_buffer_bytes=max_buffer_bytes,
            max_feed_bytes=max_feed_bytes,
        )
        self._active_client: Any = None
        self._attempt = 0
        # (channel_id, event_type) -> last dispatched eventState
        self._last_state: dict[tuple[str, str], str] = {}

    async def run(self) -> None:
        """Connect and dispatch events until stopped or cancelled."""
        try:
            while not self.stopped:
                try:
                    await self._consume_once()
                except Exception as exc:  # noqa: BLE001 - must not kill the loop
                    _LOGGER.debug(
                        "alertStream %s failed (%s: %s); reconnecting",
                        self._path, type(exc).__name__, exc,
                    )
                if self.stopped:
                    break
                delay = self._next_delay()
                await asyncio.sleep(delay)
        finally:
            await self._close_active()

    async def stop(self, task: Any = None) -> None:
        """Mark stopped, cancel the task, and close the client."""
        self.stopped = True
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._close_active()

    async def _consume_once(self) -> None:
        client = self._client_factory()
        self._active_client = client
        try:
            async for chunk in client.stream_bytes(self._path):
                if self.stopped or not chunk:
                    continue
                events = self.parser.feed(chunk)
                if events:
                    # Data is flowing: reset backoff so a healthy long-lived
                    # stream doesn't accumulate retry delay.
                    self._attempt = 0
                    self.current_backoff = self.reconnect_initial
                    self._dispatch(events)
        finally:
            await self._close_active()

    def _dispatch(self, events: list[Event]) -> None:
        for ev in events:
            if self.stopped:
                return
            key = (ev.channel_id, ev.event_type)
            if self._last_state.get(key) == ev.event_state:
                continue
            self._last_state[key] = ev.event_state
            try:
                self._on_event(ev)
            except Exception as exc:  # noqa: BLE001
                # A broken consumer (e.g. one entity raising) must not take
                # down the whole stream for every other channel.
                _LOGGER.warning(
                    "alertStream event callback failed for %s/%s: %s",
                    ev.channel_id, ev.event_type, exc,
                )

    def _next_delay(self) -> float:
        delay = min(
            self.reconnect_initial * (2 ** self._attempt), self.reconnect_max
        )
        self._attempt += 1
        self.current_backoff = delay
        self.backoff_history.append(delay)
        return delay

    async def _close_active(self) -> None:
        client = self._active_client
        self._active_client = None
        if client is None:
            return
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - closing is best-effort
            _LOGGER.debug("alertStream client close failed: %s", exc)
