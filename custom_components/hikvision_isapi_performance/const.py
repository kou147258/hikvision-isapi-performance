"""Constants for the Hikvision ISAPI integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hikvision_isapi_performance"
MANUFACTURER: Final = "Hikvision"

# ---- Polling defaults ----

# v0.6.11: changed default port 443 → 80. Most V5.x firmware serves
# ISAPI on plain HTTP at 80 by default; 443 + HTTPS is opt-in via the
# device's web-server settings. Users who already have it working on
# 443 + HTTPS can re-configure (the value is persisted per-entry).
DEFAULT_PORT: Final = 80
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 300

# ---- Network ----

# Connection timeout for a single HTTP request (seconds)
DEFAULT_REQUEST_TIMEOUT: Final = 10

# ---- Config flow fields ----

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_VERIFY_SSL: Final = "verify_ssl"
# v0.6.10: explicit HTTP/HTTPS selector. Pre-v0.6.10 we guessed based
# on port (443 → https, anything else → http), but some Hikvision
# firmware serves ISAPI over HTTP on port 443 and others serve over
# HTTPS on a non-standard port. Let the user pick explicitly.
CONF_USE_HTTPS: Final = "use_https"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# ---- ISAPI paths ----

ISAPI_SYSTEM_DEVICE_INFO: Final = "/ISAPI/System/deviceInfo"
ISAPI_SYSTEM_STATUS: Final = "/ISAPI/System/status"
ISAPI_SYSTEM_TIME: Final = "/ISAPI/System/time"
ISAPI_SYSTEM_NETWORK_INTERFACES: Final = (
    "/ISAPI/System/Network/interfaces"
)
ISAPI_SYSTEM_REBOOT: Final = "/ISAPI/System/reboot"
# v0.6.28: capability probe endpoint. Returns the device's
# supported ISAPI features and its canonical ``SupportDeviceType``
# list. Cross-checks the ``deviceType`` string returned by
# ``deviceInfo`` so we can detect new / unrecognised device types
# (helpful for IPC/NVR/DVR edge cases on obscure firmware).
ISAPI_SYSTEM_CAPABILITIES: Final = "/ISAPI/System/capabilities"

# Channel / camera streams
ISAPI_INPUT_PROXY_CHANNELS: Final = (
    "/ISAPI/ContentMgmt/InputProxy/channels"
)
ISAPI_INPUT_PROXY_CHANNELS_STATUS: Final = (
    "/ISAPI/ContentMgmt/InputProxy/channels/{id}/status"
)
ISAPI_STREAMING_CHANNELS: Final = "/ISAPI/Streaming/channels"
# Per-channel status for IPCs (streaming-side). NVRs use the
# ContentMgmt/InputProxy variant instead.
ISAPI_STREAMING_CHANNELS_STATUS: Final = (
    "/ISAPI/Streaming/channels/{id}/status"
)
# NVR-side: proxy endpoint to grab a snapshot of a mounted IPC
# channel. IPC's own /Streaming/channels/{id}/picture returns
# HTTP 400 on NVRs (verified against DS-7708-I4 / DS-8632-I8 in
# the user fleet). Use this proxy endpoint on NVRs.
ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS: Final = (
    "/ISAPI/ContentMgmt/StreamingProxy/channels"
)
ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE: Final = (
    "/ISAPI/ContentMgmt/StreamingProxy/channels/{id}/picture"
)

# Storage (NVR / DVR) — Hikvision has TWO storage endpoints
ISAPI_CONTENT_MGMT_STORAGE: Final = "/ISAPI/ContentMgmt/storage"
# Old V4 NVRs (DS-7708-I4 / DS-8632-I8) don't implement
# /ContentMgmt/storage; they only expose the legacy
# /System/Storage/hardDisks. We try ContentMgmt first, fall
# back to System on 404.
ISAPI_SYSTEM_STORAGE_HARDDISKS: Final = (
    "/ISAPI/System/Storage/hardDisks"
)

# Event streaming
ISAPI_EVENT_ALERT_STREAM: Final = (
    "/ISAPI/Event/notification/alertStream"
)

# PTZ
ISAPI_PTZ_CTRL_CHANNELS: Final = "/ISAPI/PTZCtrl/channels"

# ---- Device type strings returned by deviceInfo.deviceType ----
# Used to route snapshot / recording endpoints to the right path
# (IPC → /Streaming/channels/..., NVR → /ContentMgmt/InputProxy/...
# for the channel list, /ContentMgmt/StreamingProxy/... for the
# snapshot of a mounted IPC).
DEVICE_TYPE_IPCAMERA: Final = "ipcamera"
DEVICE_TYPE_NETWORK_VIDEO_RECORDER: Final = "networkvideorecorder"
DEVICE_TYPE_DVR: Final = "dvr"
