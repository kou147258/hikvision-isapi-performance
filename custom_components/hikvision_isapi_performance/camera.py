"""Camera platform for Hikvision ISAPI.

Each detected channel becomes one ``Camera`` entity that streams a
single JPEG request to HA's camera component. The HA frontend uses
the returned bytes to display a still image (refreshed by the
camera component's standard ``async_camera_image`` polling).

The snapshot endpoint differs by device type:

- **IPC** (``deviceType=IPCamera``) — ``/ISAPI/Streaming/channels/{id}/picture``
  on the device itself.
- **NVR / DVR** (``deviceType=NetworkVideoRecorder`` / ``DVR``) — IPC
  channels are mounted on the NVR, not local. The NVR-side proxy
  endpoint ``/ISAPI/ContentMgmt/StreamingProxy/channels/{id}/picture``
  returns the same JPEG. Using the IPC endpoint on an NVR returns
  HTTP 400 (verified against DS-7708-I4 / DS-8632-I8 in the user
  fleet).

The device type comes from the coordinator's normalized
``device_type`` field (``ipcamera`` / ``networkvideorecorder`` /
``dvr``). A future v0.2+ release can add a continuous MJPEG stream
via ``/ISAPI/Streaming/channels/{id}/httppreview`` for live video,
but HA's built-in camera component only supports still images so
that's out of scope here.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEVICE_TYPE_DVR,
    DEVICE_TYPE_NETWORK_VIDEO_RECORDER,
    DOMAIN,
    ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE,
    ISAPI_STREAMING_CHANNELS,
)
from .coordinator import HikvisionISAPICoordinator
from .entity import HikvisionISAPIEntity
from .isapi_client import ISAPIConnectionError, ISAPIClient, ISAPIError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities from the coordinator's channel list."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]

    # v0.1.23 pattern (carried over from hikvision_snmp v0.1.23):
    # do not wait for the first refresh; the coordinator's normal poll
    # cycle populates data in the background. Register a listener for
    # late-arriving channels so we don't miss any.
    entities = [
        HikvisionISAPICamera(coordinator, entry, ch)
        for ch in coordinator.channels
    ]
    async_add_entities(entities)

    if not getattr(coordinator, "_hikvision_isapi_performance_camera_added", False):
        coordinator._hikvision_isapi_performance_camera_added = False  # type: ignore[attr-defined]
        coordinator.async_add_listener(
            _make_camera_listener(
                hass, entry, coordinator, async_add_entities
            )
        )


def _make_camera_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: HikvisionISAPICoordinator,
    async_add_entities: AddEntitiesCallback,
):
    """Coordinator listener: add camera entities when channels arrive late.

    v0.7.3: was ``async def`` and therefore never ran (HA calls these
    callbacks synchronously and discards the result). Since platforms are
    set up before the first refresh, ``coordinator.channels`` is empty at
    setup time, so this dead listener meant NO camera entity was ever
    created on a fresh install.
    """

    def _on_update() -> None:
        # One-shot guard: without this the listener re-adds the same
        # cameras on EVERY coordinator refresh.
        if getattr(coordinator, "_hikvision_isapi_performance_camera_added", False):
            return
        if coordinator.data is None:
            return
        new_entities = [
            HikvisionISAPICamera(coordinator, entry, ch)
            for ch in coordinator.channels
        ]
        if not new_entities:
            return
        coordinator._hikvision_isapi_performance_camera_added = True  # type: ignore[attr-defined]
        async_add_entities(new_entities)

    return _on_update


class HikvisionISAPICamera(HikvisionISAPIEntity, Camera):
    """A still-image camera entity backed by Hikvision's /picture endpoint."""

    _attr_translation_key = "camera"

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel: dict[str, Any],
    ) -> None:
        HikvisionISAPIEntity.__init__(self, coordinator, entry)
        Camera.__init__(self)
        self._channel = channel
        self._attr_unique_id = f"{entry.entry_id}_camera_{channel['id']}"
        self._attr_translation_key = "camera"
        # Use channel name (e.g. "Camera 1") as the entity name suffix.
        self._attr_name = channel.get("name") or f"Channel {channel['id']}"

    @property
    def channel_id(self) -> str:
        return self._channel["id"]

    def _stream_id(self) -> str:
        """Return the snapshot stream id for this channel.

        Hikvision numbers streams ``{channel}{stream}`` where the main
        stream is ``01``: channel 1 → ``101``, channel 2 → ``201``,
        channel 10 → ``1001``.

        On NVR/DVR ``coordinator.channels`` comes from
        ``InputProxyChannelList`` whose ids are plain ``"1".."N"``, so the
        convention must be applied. Probed on the user's DS-7708N-I4:
        ``.../StreamingProxy/channels/1/picture`` → HTTP 503,
        ``.../StreamingProxy/channels/101/picture`` → HTTP 200 + 30142 B
        JPEG.

        On IPCs the channel list comes from ``/Streaming/channels``
        itself, so the ids are already in streaming form (``"1"``,
        ``"2"``, ``"3"`` on the DS-FB2127) and are returned untouched —
        rewriting them would break a path that already works.
        """
        cid = str(self.channel_id).strip()
        if self._is_recorder() and cid.isdigit():
            return f"{int(cid)}01"
        return cid

    def _is_recorder(self) -> bool:
        """True for NVR and DVR — devices that host remote IP channels."""
        dt = ""
        if self.coordinator.data is not None:
            dt = self.coordinator.data.device_type
        return dt in (DEVICE_TYPE_NETWORK_VIDEO_RECORDER, DEVICE_TYPE_DVR)

    def _snapshot_path(self, width: int | None = None, height: int | None = None) -> str:
        """Build the ISAPI snapshot path for this camera.

        v0.7.4: DVR was falling through to the IPC branch because the
        guard tested only ``DEVICE_TYPE_NETWORK_VIDEO_RECORDER``, even
        though this module's own docstring says "NVR / DVR". On the
        user's DS-7708N-I4 (``deviceType=DVR``) the direct endpoint
        returns HTTP 400, so no NVR/DVR camera ever rendered an image.

        Both recorder types now use the proxy endpoint.
        """
        stream_id = self._stream_id()
        if self._is_recorder():
            path = ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE.format(
                id=stream_id
            )
        else:
            path = f"{ISAPI_STREAMING_CHANNELS}/{stream_id}/picture"

        # Hikvision ignores these, but forward them so the request is
        # self-documenting for third-party integrators / packet captures.
        if width or height:
            qs = []
            if width:
                qs.append(f"videoResolutionWidth={int(width)}")
            if height:
                qs.append(f"videoResolutionHeight={int(height)}")
            path = f"{path}?{'&'.join(qs)}"
        return path

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return a single still frame from the device's snapshot endpoint.

        Endpoint selection lives in ``_snapshot_path`` (v0.7.4) so it can
        be unit-tested without issuing a network request:

        - IPC → ``/ISAPI/Streaming/channels/{id}/picture`` (direct)
        - NVR / DVR → ``/ISAPI/ContentMgmt/StreamingProxy/channels/{stream}/picture``
          (the recorder-side proxy for a mounted IP channel)

        Hikvision's ``/picture`` endpoint ignores ``width`` / ``height``
        and returns the full-resolution JPEG; the HA frontend scales it
        to fit the card. They are forwarded for documentation only.
        """
        coordinator: HikvisionISAPICoordinator = self.coordinator
        if coordinator.data is None:
            return None
        path = self._snapshot_path(width=width, height=height)

        try:
            async with ISAPIClient(
                host=coordinator._host,
                port=coordinator._port,
                username=coordinator._username,
                password=coordinator._password,
                verify_ssl=coordinator._verify_ssl,
                use_https=coordinator._use_https,
                timeout=10,
            ) as client:
                data = await client.get_bytes(path)
        except (ISAPIConnectionError, ISAPIError) as exc:
            _LOGGER.debug("Camera image fetch failed for %s: %s", path, exc)
            return None

        # Some firmwares answer 200 with an XML error body (e.g. the
        # 503 "Device Busy" page seen while probing the NVR) instead of
        # raising. Returning that as an "image" makes HA's card show a
        # broken preview, so verify the JPEG magic bytes first.
        if not data or data[:2] != b"\xff\xd8":
            _LOGGER.debug(
                "Camera snapshot for %s was not a JPEG (%d bytes); discarding",
                path,
                len(data or b""),
            )
            return None
        return data
