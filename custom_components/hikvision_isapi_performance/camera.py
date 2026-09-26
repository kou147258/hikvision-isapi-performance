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
    async def _on_update() -> None:
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

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return a single still frame from the device's snapshot endpoint.

        Endpoint is chosen by ``coordinator.data.device_type``:
        - IPC → ``/ISAPI/Streaming/channels/{id}/picture`` (direct)
        - NVR / DVR → ``/ISAPI/ContentMgmt/StreamingProxy/channels/{id}/picture``
          (NVR-side proxy that grabs a snapshot of the mounted IPC
          channel — the IPC endpoint returns 400 on NVRs)

        Hikvision's ``/picture`` endpoint ignores ``width`` /
        ``height`` parameters and returns the full-resolution JPEG; the
        HA frontend scales it to fit the card. We forward
        ``width`` / ``height`` via query string for documentation only.
        """
        coordinator: HikvisionISAPICoordinator = self.coordinator
        if coordinator.data is None:
            return None
        device_type = coordinator.data.device_type
        if device_type == DEVICE_TYPE_NETWORK_VIDEO_RECORDER:
            # NVR — use the proxy endpoint to grab a snapshot of a
            # mounted IPC channel. The constant contains a Python
            # ``{id}`` placeholder; format() resolves it to the
            # channel id at request time.
            path = ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE.format(
                id=self.channel_id
            )
        else:
            # IPC / DVR — direct local endpoint.
            path = f"{ISAPI_STREAMING_CHANNELS}/{self.channel_id}/picture"
        # Add width / height query params for documentation; Hikvision
        # ignores them but third-party integrators might check.
        if width or height:
            qs = []
            if width:
                qs.append(f"videoResolutionWidth={int(width)}")
            if height:
                qs.append(f"videoResolutionHeight={int(height)}")
            path = f"{path}?{'&'.join(qs)}"

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
                return await client.get_bytes(path)
        except (ISAPIConnectionError, ISAPIError) as exc:
            _LOGGER.debug("Camera image fetch failed for %s: %s", path, exc)
            return None
