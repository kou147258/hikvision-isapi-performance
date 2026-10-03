"""v0.8 复测发现：IPC「在线」二值传感器全部误报 off。

真机实测证据（probe_status_id.py / probe_online_field.py，12 台机群）：

  IPC 的 ``/ISAPI/Streaming/channels/{id}/status`` 返回的**不是**通道状态，
  而是 ``StreamingSessionStatusList``（活动流会话列表，元素是
  ``<StreamingSessionStatus><clientAddress><ipAddress>…``）。这个响应里
  **根本没有 ``<online>`` 字段**。

  而 ``/ISAPI/Streaming/channels`` 列表明确返回 ``<enabled>true</enabled>``
  （IPC 176.10 三个流 101/102/103 全 enabled=true）。

链路缺陷：
  1. ``_parse_channel_status*`` 对缺失的 ``<online>`` 返回 ``False``
     （把"设备没报告"当成了"设备说它离线"）。
  2. ``_async_update_data`` 用 ``ch["online"] = ch_status["online"]``
     **无条件覆盖** streaming 列表里正确的 True。
  → 结果：每台在线的 IPC 都显示"离线"。

这与 v0.7.4 修 ``recording`` 的缺陷**完全同构**：当时把"字段缺失"的
False 改成 None（三态），但 online 漏了同样处理。

修复契约：
  1. ``_parse_channel_status`` / ``_parse_channel_status_extended``：
     ``<online>`` 缺失 → ``None``；``true`` → True；``false`` → False。
  2. 抽出可测的 ``_merge_channel_status(ch, ch_status)``：online 只在
     status 明确报告（非 None）时覆盖，与 recording 同规则。保留 v0.6.12
     的意图——status 明确说 False 时要能盖掉列表的 True。
  3. ``HikvisionISAPIChannelOnlineBinarySensor.is_on``：三态，None → unknown。

不写设备；测试用最小忠实 XML（不含真实客户端 IP）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import conftest  # noqa: F401,E402  installs HA stubs on import

from custom_components.hikvision_isapi_performance import (  # noqa: E402
    binary_sensor as binary_mod,
)
from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    _merge_channel_status,
    _parse_channel_status,
    _parse_channel_status_extended,
)


def _strip_ns(xml: str) -> str:
    import re
    return re.sub(r'\s+xmlns(?::\w+)?\s*=\s*["\'][^"\']*["\']', "", xml)


# IPC /Streaming/channels/1/status 的真实形态：会话列表，无 <online>。
# 客户端 IP 已脱敏为占位，保留结构（root tag + 嵌套），忠实复现 bug。
IPC_SESSION_STATUS_LIST = """<StreamingSessionStatusList version="2.0">
<StreamingSessionStatus>
<clientAddress><ipAddress>10.0.0.9</ipAddress></clientAddress>
</StreamingSessionStatus>
<StreamingSessionStatus>
<clientAddress><ipAddress>10.0.0.8</ipAddress></clientAddress>
</StreamingSessionStatus>
</StreamingSessionStatusList>"""

# NVR per-channel status：明确带 <online>true</online>。
NVR_STATUS_ONLINE_TRUE = """<InputProxyChannelStatus>
<id>1</id><online>true</online><recordStatus>idle</recordStatus>
</InputProxyChannelStatus>"""

# 明确带 <online>false</online> 的形态（设备确实报告离线）。
STATUS_ONLINE_FALSE = """<InputProxyChannelStatus>
<id>1</id><online>false</online>
</InputProxyChannelStatus>"""


# ---- 1. 解析层：缺失 <online> 不得谎报 False -------------------------- #

def test_session_status_list_online_is_none_not_false():
    """IPC 会话列表无 <online> → online 必须是 None，不是 False。"""
    root = ET.fromstring(_strip_ns(IPC_SESSION_STATUS_LIST))
    st = _parse_channel_status(root)
    assert st["online"] is None, (
        f"会话列表无 <online>，却解析成 {st['online']!r}；"
        "None 才表示'设备没报告'"
    )


def test_session_status_list_extended_online_is_none():
    root = ET.fromstring(_strip_ns(IPC_SESSION_STATUS_LIST))
    st = _parse_channel_status_extended(root)
    assert st["online"] is None


def test_explicit_online_true_still_true():
    """回归：字段存在且 true 时仍解析 True。"""
    st = _parse_channel_status(ET.fromstring(_strip_ns(NVR_STATUS_ONLINE_TRUE)))
    assert st["online"] is True
    st2 = _parse_channel_status_extended(
        ET.fromstring(_strip_ns(NVR_STATUS_ONLINE_TRUE)))
    assert st2["online"] is True


def test_explicit_online_false_still_false():
    """回归：字段存在且 false 时解析 False（设备确实报告离线）。"""
    st = _parse_channel_status(ET.fromstring(_strip_ns(STATUS_ONLINE_FALSE)))
    assert st["online"] is False


# ---- 2. 合并层：None 不得覆盖列表已有的 online ----------------------- #

def test_merge_does_not_clobber_online_with_none():
    """核心 bug：status 的 online=None 不得覆盖 streaming 列表的 True。"""
    ch = {"id": "1", "name": "摄像机12", "online": True}
    ch_status = _parse_channel_status_extended(
        ET.fromstring(_strip_ns(IPC_SESSION_STATUS_LIST)))
    _merge_channel_status(ch, ch_status)
    assert ch["online"] is True, (
        f"IPC 在线却被覆盖成 {ch['online']!r}；status 无 <online> 时必须保留列表值"
    )


def test_merge_trusts_explicit_status_online_false():
    """保留 v0.6.12 意图：status 明确报 False 时盖掉列表的 True。"""
    ch = {"id": "1", "name": "x", "online": True}
    ch_status = _parse_channel_status_extended(
        ET.fromstring(_strip_ns(STATUS_ONLINE_FALSE)))
    _merge_channel_status(ch, ch_status)
    assert ch["online"] is False


def test_merge_trusts_explicit_status_online_true():
    ch = {"id": "1", "name": "x", "online": False}
    ch_status = _parse_channel_status_extended(
        ET.fromstring(_strip_ns(NVR_STATUS_ONLINE_TRUE)))
    _merge_channel_status(ch, ch_status)
    assert ch["online"] is True


def test_merge_recording_tristate_preserved():
    """合并抽取后，recording 的三态规则（v0.7.4）不得回退。"""
    ch = {"id": "1", "name": "x", "online": True, "recording": True}
    # 会话列表 recording 也是 None（无 recordStatus）→ 不得覆盖 True
    ch_status = _parse_channel_status_extended(
        ET.fromstring(_strip_ns(IPC_SESSION_STATUS_LIST)))
    _merge_channel_status(ch, ch_status)
    assert ch["recording"] is True, "status 无 recordStatus 时不得覆盖已有录像状态"


# ---- 3. 实体层：online 三态 ------------------------------------------ #

class _Coord:
    def __init__(self, data):
        self.data = data


class _Entry:
    entry_id = "online_test"
    title = "t"
    unique_id = "u"
    data = {}
    options = {}
    domain = "hikvision_isapi_performance"


def _data(channels):
    d = type("D", (), {})()
    d.channels = channels
    return d


def test_online_binary_sensor_tristate_none_is_unknown():
    """channel online=None → is_on 返回 None（HA 显示 unknown），不是 False。"""
    ent = binary_mod.HikvisionISAPIChannelOnlineBinarySensor(
        _Coord(_data([{"id": "1", "name": "摄像机12", "online": None}])),
        _Entry(), "1", "摄像机12")
    assert ent.is_on is None, (
        f"online 未知却返回 {ent.is_on!r}；应返回 None 让 HA 显示 unknown"
    )


def test_online_binary_sensor_true():
    ent = binary_mod.HikvisionISAPIChannelOnlineBinarySensor(
        _Coord(_data([{"id": "1", "name": "摄像机12", "online": True}])),
        _Entry(), "1", "摄像机12")
    assert ent.is_on is True


def test_online_binary_sensor_false():
    ent = binary_mod.HikvisionISAPIChannelOnlineBinarySensor(
        _Coord(_data([{"id": "1", "name": "摄像机12", "online": False}])),
        _Entry(), "1", "摄像机12")
    assert ent.is_on is False


def test_online_binary_sensor_no_data_is_unknown():
    """coordinator.data 为 None（首次刷新前）→ unknown。"""
    ent = binary_mod.HikvisionISAPIChannelOnlineBinarySensor(
        _Coord(None), _Entry(), "1", "摄像机12")
    assert ent.is_on is None


def test_online_binary_sensor_channel_absent_is_unknown():
    """请求的通道 id 不在 channels 里 → unknown（不谎报离线）。"""
    ent = binary_mod.HikvisionISAPIChannelOnlineBinarySensor(
        _Coord(_data([{"id": "2", "name": "别的", "online": True}])),
        _Entry(), "1", "摄像机12")
    assert ent.is_on is None
