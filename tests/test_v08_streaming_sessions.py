"""v0.8：活动流会话数传感器（streaming sessions）。

实测（probe_sessions.py，12 台，2026-10-04）：

  * 设备级 ``/ISAPI/Streaming/sessions`` **12/12 全部失败** → 不可用
  * IPC 的 ``/ISAPI/Streaming/channels/{id}/status`` 返回
    ``StreamingSessionStatusList``，会话数 2~7（176.10=7, 176.12=4,
    176.13=4, 176.16=4, 176.17=4, 176.18=4, 176.51=2, 176.52=2, 176.53=2）
  * NVR/DVR 的 ``/ContentMgmt/InputProxy/channels/{id}/status`` 返回
    ``InputProxyChannelStatus``，**0 个会话**（3/3 台）

两个由此确定的设计约束：

1. **只在响应确实是会话列表时建实体**。判据是根标签为
   ``StreamingSessionStatusList``，而不是"会话数 > 0"——0 个会话本身
   是有意义的信息（没人在看流），而 NVR 根本不提供这个端点形态。
   → 9 台 IPC 建，3 台 NVR 不建（避免永久 0/unknown 噪音实体）。

2. **绝不暴露客户端 IP**。会话结构里唯一的 payload 就是
   ``clientAddress/ipAddress``（实测无 sessionId / streamType）。
   把它做成实体会公开用户内网拓扑，价值远低于隐私代价。只做计数。

零额外 HTTP 开销：IPC 的这个 status 端点本来就在每轮刷新中被请求
（primary_status_fmt），此前会话数据被解析器直接丢弃。
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import conftest  # noqa: F401,E402  installs the HA stubs

from custom_components.hikvision_isapi_performance import (  # noqa: E402
    capabilities as caps,
)
from custom_components.hikvision_isapi_performance import sensor as sensor_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    _merge_channel_status,
    _parse_channel_status_extended,
)


# IPC /Streaming/channels/1/status 的真实形态：3 个会话、4 个客户端 IP
# （IP 已脱敏为占位，保留结构；实测 176.10 有 7 会话 / 4 唯一 IP）。
IPC_SESSIONS_3 = """<StreamingSessionStatusList version="2.0">
<StreamingSessionStatus>
<clientAddress><ipAddress>10.0.0.22</ipAddress></clientAddress>
</StreamingSessionStatus>
<StreamingSessionStatus>
<clientAddress><ipAddress>10.0.0.65</ipAddress></clientAddress>
</StreamingSessionStatus>
<StreamingSessionStatus>
<clientAddress><ipAddress>192.168.9.10</ipAddress></clientAddress>
</StreamingSessionStatus>
</StreamingSessionStatusList>"""

# 空会话列表（形态正确但无人拉流）
IPC_SESSIONS_0 = """<StreamingSessionStatusList version="2.0">
</StreamingSessionStatusList>"""

# NVR /ContentMgmt/InputProxy/channels/1/status 的真实形态：不是会话列表
NVR_PROXY_STATUS = """<InputProxyChannelStatus>
<id>1</id>
<sourceInputPortDescriptor>
<proxyProtocol>HIKVISION</proxyProtocol>
<ipAddress>10.0.0.10</ipAddress>
</sourceInputPortDescriptor>
<online>true</online>
</InputProxyChannelStatus>"""


# ---- 1. 解析层 ---------------------------------------------------------- #

def test_session_list_counts_sessions():
    """会话列表 → 返回会话数。"""
    root = ET.fromstring(IPC_SESSIONS_3)
    assert caps.parse_streaming_sessions(root) == 3


def test_empty_session_list_is_zero_not_none():
    """空会话列表 → 0（有意义：没人在拉流），不是 None。"""
    root = ET.fromstring(IPC_SESSIONS_0)
    assert caps.parse_streaming_sessions(root) == 0


def test_non_session_list_returns_none():
    """NVR 的 InputProxyChannelStatus → None（该端点形态不提供会话数据）。"""
    root = ET.fromstring(NVR_PROXY_STATUS)
    assert caps.parse_streaming_sessions(root) is None


def test_none_root_returns_none():
    assert caps.parse_streaming_sessions(None) is None


def test_session_parser_never_exposes_client_ips():
    """隐私钉子：返回值只能是 int/None，绝不能是含 IP 的结构。"""
    root = ET.fromstring(IPC_SESSIONS_3)
    result = caps.parse_streaming_sessions(root)
    assert isinstance(result, int), (
        f"会话解析必须只返回计数（int），却返回了 {type(result).__name__}；"
        "暴露客户端 IP 等于公开用户内网拓扑"
    )
    assert "10.0.0.22" not in repr(result)


# ---- 2. 合并层：None 不得写入通道 ------------------------------------- #

def test_merge_writes_session_count_when_available():
    ch = {"id": "1", "name": "摄像机12", "online": True}
    st = _parse_channel_status_extended(ET.fromstring(IPC_SESSIONS_3))
    _merge_channel_status(ch, st)
    assert ch.get("streaming_sessions") == 3


def test_merge_writes_zero_session_count():
    """0 是有效值，必须写入（区别于 None 的"端点不提供"）。"""
    ch = {"id": "1", "name": "摄像机12", "online": True}
    st = _parse_channel_status_extended(ET.fromstring(IPC_SESSIONS_0))
    _merge_channel_status(ch, st)
    assert ch.get("streaming_sessions") == 0


def test_merge_omits_session_key_for_nvr_shape():
    """NVR 形态 → 不写 streaming_sessions 键（实体据此不创建）。"""
    ch = {"id": "1", "name": "摄像机12", "online": True}
    st = _parse_channel_status_extended(ET.fromstring(NVR_PROXY_STATUS))
    _merge_channel_status(ch, st)
    assert "streaming_sessions" not in ch, (
        "NVR 的 InputProxyChannelStatus 不含会话数据，写入 None/0 都会"
        "造出一个永久无意义的实体"
    )


# ---- 3. 实体层 ---------------------------------------------------------- #

class _Coord:
    def __init__(self, data):
        self.data = data
        self.duplicate_channels = set()
        self.storage = {}


class _Entry:
    entry_id = "sess"
    title = "t"
    unique_id = "u"
    data = {}
    options = {}
    domain = "hikvision_isapi_performance"


def _data(channels):
    d = type("D", (), {})()
    d.channels = channels
    return d


def _session_entities(channel: dict) -> list:
    coord = _Coord(_data([channel]))
    ents = sensor_mod._build_per_channel_entities(coord, _Entry(), channel)
    return [e for e in ents
            if e.entity_description.key.endswith("_streaming_sessions")]


def test_session_sensor_built_for_ipc_channel():
    """IPC 通道（有 streaming_sessions）→ 建实体，值正确。"""
    ents = _session_entities(
        {"id": "1", "name": "摄像机12", "online": True, "streaming_sessions": 7})
    assert len(ents) == 1
    ent = ents[0]
    assert ent.native_value == 7
    assert "摄像机12" in ent.entity_description.name


def test_session_sensor_not_built_for_nvr_channel():
    """NVR 通道（无 streaming_sessions 键）→ 不建实体。"""
    ents = _session_entities({"id": "1", "name": "摄像机12", "online": True})
    assert ents == [], (
        "无会话数据的通道不该建实体，否则是永久 unknown 噪音"
    )


def test_session_sensor_zero_is_built():
    """streaming_sessions=0 → 仍建实体（0 是有效读数）。"""
    ents = _session_entities(
        {"id": "1", "name": "摄像机12", "online": True, "streaming_sessions": 0})
    assert len(ents) == 1
    assert ents[0].native_value == 0


def test_session_sensor_metadata():
    """计数传感器应有 MEASUREMENT（可出统计图表）与 DIAGNOSTIC 分类。"""
    ents = _session_entities(
        {"id": "1", "name": "摄像机12", "online": True, "streaming_sessions": 4})
    desc = ents[0].entity_description
    assert desc.state_class == "measurement"
    assert desc.entity_category == "diagnostic"
    # 动态多实例实体不得设 translation_key（否则摄像机名被遮蔽）
    assert desc.translation_key is None


def test_session_sensor_respects_dedup_disable():
    """去重命中的通道，会话传感器同样默认禁用。"""
    ch = {"id": "1", "name": "摄像机12", "online": True,
          "streaming_sessions": 4}
    coord = _Coord(_data([ch]))
    coord.duplicate_channels = {"1"}
    ents = [e for e in sensor_mod._build_per_channel_entities(
        coord, _Entry(), ch)
        if e.entity_description.key.endswith("_streaming_sessions")]
    assert len(ents) == 1
    assert ents[0].entity_description.entity_registry_enabled_default is False
