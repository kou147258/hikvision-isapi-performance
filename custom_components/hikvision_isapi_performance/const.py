"""Constants for the Hikvision ISAPI Performance integration."""

from __future__ import annotations

DOMAIN = "hikvision_isapi_performance"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
CONF_USE_HTTPS = "use_https"

DEFAULT_PORT = 80
DEFAULT_SCAN_INTERVAL = 30

# ISAPI endpoints
ISAPI_SYSTEM_DEVICE_INFO = "/ISAPI/System/deviceInfo"
ISAPI_SYSTEM_STATUS = "/ISAPI/System/status"
ISAPI_SYSTEM_TIME = "/ISAPI/System/time"
ISAPI_SYSTEM_CAPABILITIES = "/ISAPI/System/capabilities"
ISAPI_CONTENT_MGMT_INPUT_PROXY_CHANNELS = (
    "/ISAPI/ContentMgmt/InputProxy/channels"
)
ISAPI_STREAMING_CHANNELS = "/ISAPI/Streaming/channels"
# [FIX #11] Removed duplicate ISAPI_STREAMING_CHANNELS_LIST — was identical
# to ISAPI_STREAMING_CHANNELS. The coordinator's _fetch_streaming now uses
# ISAPI_STREAMING_CHANNELS directly.
ISAPI_CONTENT_MGMT_STORAGE = "/ISAPI/ContentMgmt/storage"
ISAPI_SYSTEM_STORAGE_HARDDISKS = "/ISAPI/System/Storage/hardDisks"
ISAPI_CONTENT_MGMT_STORAGE_HDDLIST = "/ISAPI/ContentMgmt/storage/hddList"
ISAPI_NETWORK_INTERFACES = "/ISAPI/System/Network/interfaces"
ISAPI_NETWORK_INTERFACES_ALT = "/ISAPI/System/network/interfaces"
ISAPI_REBOOT = "/ISAPI/System/reboot"
ISAPI_PTZ_CONTINUOUS = "/ISAPI/PTZCtrl/channels/{channel}/continuous"
ISAPI_PICTURE = "/ISAPI/Streaming/channels/{id}/picture"
