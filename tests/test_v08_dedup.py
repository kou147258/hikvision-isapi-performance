"""v0.8 步骤11：序列号去重（9 台 IPC 同时是 NVR 通道）。

真机实测（analyze_dedup.py 输出）：
  NVR 176.64 的 13 个通道中，**9 个通道的 serialNumber 与清单里 9 台独立
  IPC 完全一致** —— 同一台物理摄像机既独立接入 HA，又作为 NVR 代理通道
  存在。两边都添加会产生两套实体（这正是用户要求"去重"的原因）。

  通道名（真实摄像机名）：ch1 摄像机12=176.10、ch3 摄像机09=176.12、
  ch4 摄像机10=176.13、ch5 摄像机14=176.16、ch9 摄像机01=176.51、
  ch10 摄像机10=176.52、ch11 摄像机03=176.53、ch12 摄像机07=176.17、
  ch13 摄像机06=176.18
  非重叠：ch2 摄像机13(176.11)、ch6/7(192.168.10.17)、ch8 摄像机11(192.168.10.12)

身份键的可靠性差异（决定匹配策略）：
  * 176.64：serialNumber 齐全 → 用序列号匹配（IP 会变，序列号不会）
  * 176.65（DS-7708N-I4 V4.1.18）：serialNumber **全为空** → 必须回退
    用 sourceInputPortDescriptor/ipAddress 匹配
  * 192.168.10.17：2 个通道是清单外设备 → 不应判为重复

用户决策 D1：NVR 侧重复通道实体 `entity_registry_enabled_default=False`
（默认禁用但保留，用户可在实体注册表手动启用），不丢数据。

前置缺口：_parse_channels 此前只保留 id/name/online/recording，
**未捕获 serialNumber 与源 IP**，去重无从判断。本步先补齐解析。

测试全部离线，用真机抓包 fixture。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import dedup  # noqa: E402
from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    HikvisionISAPIData,
    _parse_channels,
)
from custom_components.hikvision_isapi_performance.const import DOMAIN  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures_v08"

# 真机实测：176.64 的 9 个重叠通道 → 独立 IPC 主机
NVR_64_DUPLICATES = {
    "1": "10.18.176.10",
    "3": "10.18.176.12",
    "4": "10.18.176.13",
    "5": "10.18.176.16",
    "9": "10.18.176.51",
    "10": "10.18.176.52",
    "11": "10.18.176.53",
    "12": "10.18.176.17",
    "13": "10.18.176.18",
}

# 真机实测：9 台独立 IPC 的序列号（来自 deviceInfo 抓包）
IPC_SERIALS = {
    "10.18.176.10": "SN-TEST-008",
    "10.18.176.12": "SN-TEST-015",
    "10.18.176.13": "SN-TEST-012",
    "10.18.176.16": "SN-TEST-001",
    "10.18.176.17": "SN-TEST-019",
    "10.18.176.18": "SN-TEST-011",
    "10.18.176.51": "SN-TEST-004",
    "10.18.176.52": "SN-TEST-005",
    "10.18.176.53": "SN-TEST-003",
}


def _proxy_root(host: str) -> ET.Element:
    files = sorted(FIXTURES.glob(f"{host.replace('.', '_')}__I_proxy__*.xml"))
    assert files, f"missing InputProxy fixture for {host}"
    return ET.fromstring(files[0].read_text(encoding="utf-8", errors="replace"))


# ── 1. 解析必须捕获身份字段 ────────────────────────────────────────


def test_parse_channels_captures_serial_number():
    """_parse_channels 必须保留 serialNumber（去重主键）。"""
    chans = _parse_channels(_proxy_root("10.18.176.64"))
    by_id = {c["id"]: c for c in chans}
    assert by_id["1"]["serial_number"] == IPC_SERIALS["10.18.176.10"]
    assert by_id["13"]["serial_number"] == IPC_SERIALS["10.18.176.18"]


def test_parse_channels_captures_source_ip():
    """_parse_channels 必须保留源 IP（serialNumber 缺失时的回退键）。"""
    chans = _parse_channels(_proxy_root("10.18.176.64"))
    by_id = {c["id"]: c for c in chans}
    assert by_id["1"]["source_ip"] == "10.18.176.10"
    # ch2 摄像机13是未独立添加的设备
    assert by_id["2"]["source_ip"] == "10.18.176.11"


def test_parse_channels_serial_empty_when_firmware_omits_it():
    """176.65（V4.1.18）不返回 serialNumber → 解析为空字符串而非崩溃。"""
    chans = _parse_channels(_proxy_root("10.18.176.65"))
    assert len(chans) == 8
    assert all(c["serial_number"] == "" for c in chans)
    # 但源 IP 必须有（回退匹配靠它）
    by_id = {c["id"]: c for c in chans}
    assert by_id["1"]["source_ip"] == "10.18.176.10"


def test_parse_channels_keeps_channel_count():
    """回归：13/8/2 通道数不受身份字段新增影响。"""
    assert len(_parse_channels(_proxy_root("10.18.176.64"))) == 13
    assert len(_parse_channels(_proxy_root("10.18.176.65"))) == 8
    assert len(_parse_channels(_proxy_root("192.168.10.17"))) == 2


# ── 2. 身份提取 ────────────────────────────────────────────────────


def test_extract_identity_from_device_info():
    """从 deviceInfo 提取身份（序列号 + 主机）。"""
    ident = dedup.extract_identity(
        {"serialNumber": "SN123", "model": "DS-2CD3T86"}, "10.18.176.51")
    assert ident["serial_number"] == "SN123"
    assert ident["host"] == "10.18.176.51"
    assert ident["model"] == "DS-2CD3T86"


def test_extract_identity_tolerates_missing_fields():
    """字段缺失不崩溃，返回空串。"""
    ident = dedup.extract_identity({}, "1.2.3.4")
    assert ident["serial_number"] == ""
    assert ident["host"] == "1.2.3.4"


# ── 3. 重复通道判定：序列号匹配 ────────────────────────────────────


def test_duplicates_by_serial_number_real_fleet():
    """真机数据：176.64 的 9 个通道按序列号判为重复。"""
    chans = _parse_channels(_proxy_root("10.18.176.64"))
    others = [
        {"serial_number": sn, "host": host}
        for host, sn in IPC_SERIALS.items()
    ]
    dups = dedup.find_duplicate_channels(chans, others)
    assert dups == set(NVR_64_DUPLICATES), f"应为 9 个通道，实得 {sorted(dups)}"


def test_non_duplicate_channels_not_flagged():
    """ch2 摄像机13、ch6/7、ch8 未独立添加 → 不得判为重复。"""
    chans = _parse_channels(_proxy_root("10.18.176.64"))
    others = [{"serial_number": sn, "host": h} for h, sn in IPC_SERIALS.items()]
    dups = dedup.find_duplicate_channels(chans, others)
    for ch in ("2", "6", "7", "8"):
        assert ch not in dups, f"通道 {ch} 被误判为重复"


def test_third_nvr_no_false_duplicates():
    """192.168.10.17 的 2 个通道是清单外设备 → 不得判为重复。"""
    chans = _parse_channels(_proxy_root("192.168.10.17"))
    others = [{"serial_number": sn, "host": h} for h, sn in IPC_SERIALS.items()]
    assert dedup.find_duplicate_channels(chans, others) == set()


# ── 4. 重复通道判定：IP 回退（序列号缺失）─────────────────────────


def test_fallback_to_ip_when_serial_empty():
    """176.65 序列号全空 → 用源 IP 匹配。

    真机 176.65 的 8 个通道源 IP 是 176.10/192.168.10.12/176.11/176.12/
    176.13/176.16/176.17/176.18，其中 6 个在独立 IPC 清单里。
    """
    chans = _parse_channels(_proxy_root("10.18.176.65"))
    others = [{"serial_number": sn, "host": h} for h, sn in IPC_SERIALS.items()]
    dups = dedup.find_duplicate_channels(chans, others)
    # 通道1=176.10, ch4=176.12, ch5=176.13, ch6=176.16, ch7=176.17, ch8=176.18
    assert dups == {"1", "4", "5", "6", "7", "8"}, f"实得 {sorted(dups)}"
    # ch2=192.168.10.12 与 ch3=176.11 未独立添加
    assert "2" not in dups and "3" not in dups


def test_serial_takes_priority_over_ip():
    """序列号可用时优先用它（IP 可能因 DHCP 变化）。"""
    chans = [
        # 序列号匹配 A，但 IP 指向 B → 应按序列号判为与 A 重复
        {"id": "1", "serial_number": "SN_A", "source_ip": "10.0.0.99"},
    ]
    others = [{"serial_number": "SN_A", "host": "10.0.0.1"}]
    assert dedup.find_duplicate_channels(chans, others) == {"1"}


def test_ip_fallback_only_when_serial_missing():
    """有序列号但不匹配时，不得仅凭 IP 判为重复（避免误判）。"""
    chans = [{"id": "1", "serial_number": "SN_X", "source_ip": "10.0.0.1"}]
    others = [{"serial_number": "SN_OTHER", "host": "10.0.0.1"}]
    assert dedup.find_duplicate_channels(chans, others) == set(), (
        "序列号明确不同，即使 IP 相同也不是同一台设备（IP 可能已复用）"
    )


def test_empty_inputs():
    assert dedup.find_duplicate_channels([], []) == set()
    assert dedup.find_duplicate_channels([{"id": "1"}], []) == set()


# ── 5. 跨 config entry 身份收集 ────────────────────────────────────


def _entry(entry_id: str, host: str, serial: str = "", model: str = ""):
    e = type("E", (), {})()
    e.entry_id = entry_id
    e.data = {
        "host": host, "username": "admin", "password": "pw",
        # v0.8: 首次成功刷新后由 __init__ 写回
        "identity_serial": serial,
        "identity_model": model,
    }
    e.options = {}
    return e


def test_collect_other_entries_excludes_self():
    """只收集**其他** entry 的身份，不含自己。"""
    hass = type("H", (), {})()
    entries = [
        _entry("nvr_entry", "10.18.176.64", "NVR_SN", "DS-8632N-I8"),
        _entry("ipc_10", "10.18.176.10", IPC_SERIALS["10.18.176.10"]),
        _entry("ipc_12", "10.18.176.12", IPC_SERIALS["10.18.176.12"]),
    ]
    hass.config_entries = type("CE", (), {
        "async_entries": lambda self, domain: entries,
    })()
    others = dedup.collect_other_identities(hass, "nvr_entry")
    hosts = {o["host"] for o in others}
    assert hosts == {"10.18.176.10", "10.18.176.12"}
    assert "10.18.176.64" not in hosts


def test_collect_other_entries_skips_unprobed():
    """身份尚未探测到的 entry（identity_serial 为空）仍可贡献 host。

    场景：用户先加了 IPC（尚未刷新完），再加 NVR。此时 IPC 的序列号未知，
    但 host 已知，NVR 侧仍可凭源 IP 回退匹配。
    """
    hass = type("H", (), {})()
    entries = [
        _entry("nvr_entry", "10.18.176.64"),
        _entry("ipc_10", "10.18.176.10"),  # serial 为空
    ]
    hass.config_entries = type("CE", (), {
        "async_entries": lambda self, domain: entries,
    })()
    others = dedup.collect_other_identities(hass, "nvr_entry")
    assert len(others) == 1
    assert others[0]["host"] == "10.18.176.10"
    assert others[0]["serial_number"] == ""


def test_collect_other_entries_empty_when_alone():
    """只有一个 entry（未同时添加 IPC 与 NVR）→ 无重复可言。"""
    hass = type("H", (), {})()
    hass.config_entries = type("CE", (), {
        "async_entries": lambda self, domain: [_entry("only", "10.18.176.64")],
    })()
    assert dedup.collect_other_identities(hass, "only") == []


# ── 6. 实体默认禁用 ────────────────────────────────────────────────


def test_should_disable_flags_duplicate_channel():
    """重复通道 → 应默认禁用。"""
    assert dedup.should_disable_channel("1", {"1", "3"}) is True


def test_should_disable_passes_unique_channel():
    """非重复通道 → 正常启用。"""
    assert dedup.should_disable_channel("2", {"1", "3"}) is False


def test_should_disable_when_no_duplicates():
    assert dedup.should_disable_channel("1", set()) is False


def test_should_disable_handles_non_string_ids():
    """通道 id 可能是 int（固件差异）→ 归一化后比较。"""
    assert dedup.should_disable_channel(1, {"1"}) is True
    assert dedup.should_disable_channel("1", {1}) is True


# ── 7. 诊断信息 ────────────────────────────────────────────────────


def test_describe_duplicates_for_diagnostics():
    """生成人类可读的重复关系说明（供日志与诊断下载）。"""
    chans = _parse_channels(_proxy_root("10.18.176.64"))
    others = [{"serial_number": sn, "host": h} for h, sn in IPC_SERIALS.items()]
    report = dedup.describe_duplicates(chans, others)
    assert len(report) == 9
    # 报告需含通道名、通道号、对应的独立设备主机
    ch1 = next(r for r in report if r["channel_id"] == "1")
    assert ch1["channel_name"] == "摄像机12"
    assert ch1["duplicate_of"] == "10.18.176.10"


def test_describe_duplicates_empty_when_none():
    chans = _parse_channels(_proxy_root("192.168.10.17"))
    others = [{"serial_number": sn, "host": h} for h, sn in IPC_SERIALS.items()]
    assert dedup.describe_duplicates(chans, others) == []


# ── 8. 接线：coordinator 计算重复集合 ──────────────────────────────
#
# 纯函数存在 ≠ 去重生效。coordinator 必须在首次刷新后：
#   a) 把自身身份写回 config entry（供其他条目读取）
#   b) 读取其他条目身份，算出本设备的重复通道集合
#   c) 唤醒监听器让平台注册时能应用默认禁用


def _entry_obj(entry_id: str, host: str, serial: str = "", model: str = ""):
    e = type("E", (), {})()
    e.entry_id = entry_id
    e.data = {
        "host": host, "username": "admin", "password": "pw",
        "identity_serial": serial, "identity_model": model,
    }
    e.options = {}
    return e


def _fake_hass(entries):
    h = type("H", (), {})()
    store = {e.entry_id: dict(e.data) for e in entries}
    h.data = {DOMAIN: {}}
    h.config_entries = type("CE", (), {
        "async_entries": lambda self, domain: entries,
        "async_update_entry": lambda self, entry, **kw: store.setdefault(
            entry.entry_id, {}).update(kw.get("data", {}) or {}),
    })()
    h._store = store
    return h


def _make_coord(hass, entry, channels):
    from custom_components.hikvision_isapi_performance import coordinator as coord_mod
    c = coord_mod.HikvisionISAPICoordinator.__new__(
        coord_mod.HikvisionISAPICoordinator)
    c._host = entry.data["host"]
    c._port = 80
    c._username = "admin"
    c._password = "pw"
    c._verify_ssl = False
    c._use_https = False
    c.hass = hass
    c.entry_id = entry.entry_id
    c.entry = entry
    c.channels = channels
    c.device_info = {}
    c.duplicate_channels = set()
    # v0.8 状态字段：测试用 __new__ 绕过 __init__，须显式初始化
    c._identity_published = False
    c._dedup_applied_signature = None
    c.wakeups = 0
    c.async_update_listeners = lambda: setattr(c, "wakeups", c.wakeups + 1)
    return c


def test_coordinator_computes_duplicate_channels():
    """真机 176.64：9 个通道与其他条目的 IPC 重复 → 记入 duplicate_channels。"""
    nvr_entry = _entry_obj("nvr", "10.18.176.64")
    others = [_entry_obj(f"ipc{h}", h, sn) for h, sn in IPC_SERIALS.items()]
    hass = _fake_hass([nvr_entry] + others)
    coord = _make_coord(hass, nvr_entry,
                        _parse_channels(_proxy_root("10.18.176.64")))
    coord.apply_dedup()
    assert coord.duplicate_channels == set(NVR_64_DUPLICATES)


def test_coordinator_no_duplicates_when_alone():
    """只有 NVR 一个条目（用户未单独添加 IPC）→ 无重复，全部正常启用。"""
    nvr_entry = _entry_obj("nvr", "10.18.176.64")
    hass = _fake_hass([nvr_entry])
    coord = _make_coord(hass, nvr_entry,
                        _parse_channels(_proxy_root("10.18.176.64")))
    coord.apply_dedup()
    assert coord.duplicate_channels == set()


def test_coordinator_dedup_wakes_listeners():
    """算出重复后必须唤醒监听器，否则已注册实体不会重新评估。"""
    nvr_entry = _entry_obj("nvr", "10.18.176.64")
    others = [_entry_obj(f"ipc{h}", h, sn) for h, sn in IPC_SERIALS.items()]
    hass = _fake_hass([nvr_entry] + others)
    coord = _make_coord(hass, nvr_entry,
                        _parse_channels(_proxy_root("10.18.176.64")))
    coord.apply_dedup()
    assert coord.wakeups >= 1


def test_coordinator_dedup_idempotent_wakes_once():
    """重复调用不得反复唤醒（每 30 秒刷新一次会持续触发）。"""
    nvr_entry = _entry_obj("nvr", "10.18.176.64")
    others = [_entry_obj(f"ipc{h}", h, sn) for h, sn in IPC_SERIALS.items()]
    hass = _fake_hass([nvr_entry] + others)
    coord = _make_coord(hass, nvr_entry,
                        _parse_channels(_proxy_root("10.18.176.64")))
    coord.apply_dedup()
    coord.apply_dedup()
    coord.apply_dedup()
    assert coord.wakeups == 1, f"结果未变时应只唤醒一次，实得 {coord.wakeups}"


def test_coordinator_publishes_own_identity():
    """本条目身份写回 entry.data，供其他条目读取（去重的前提）。"""
    nvr_entry = _entry_obj("nvr", "10.18.176.64")
    hass = _fake_hass([nvr_entry])
    coord = _make_coord(hass, nvr_entry, [])
    coord.device_info = {
        "serialNumber": "SN-TEST-010",
        "model": "DS-8632N-I8",
    }
    coord.publish_identity()
    updated = hass._store["nvr"]
    assert updated["identity_serial"] == (
        "SN-TEST-010")
    assert updated["identity_model"] == "DS-8632N-I8"


def test_publish_identity_noop_without_serial():
    """deviceInfo 未就绪时不写空值（避免覆盖已探测到的身份）。"""
    nvr_entry = _entry_obj("nvr", "10.18.176.64", serial="EXISTING_SN")
    hass = _fake_hass([nvr_entry])
    coord = _make_coord(hass, nvr_entry, [])
    coord.device_info = {}
    coord.publish_identity()
    assert hass._store["nvr"]["identity_serial"] == "EXISTING_SN"


# ── 9. 接线：实体默认禁用 ──────────────────────────────────────────


class _DedupCoord:
    """最小假 coordinator，用于验证实体默认禁用标志。"""

    def __init__(self, duplicate_channels, channels=None):
        self.duplicate_channels = duplicate_channels
        self.channels = channels or []
        self.data = HikvisionISAPIData(
            device_info={}, system_status={}, channels=channels or [],
            capabilities={}, storage={}, network_interfaces=[],
            streaming_channel_detail={},
        )
        self.storage: dict = {}
        self.motion_detection: dict = {}
        self.event_state: dict = {}
        self.recording_status: dict = {}
        self.network_interfaces: list = []
        self.device_type = "networkvideorecorder"
        self.device_info: dict = {}
        self.capabilities: dict = {}
        self.streaming_channel_detail: dict = {}
        self.system_capabilities: dict = {}
        self.system_status: dict = {}
        self.unique_id = f"{DOMAIN}_dd"
        self._host = "10.18.176.64"
        self._port = 80
        self._username = "admin"
        self._password = "pw"
        self._verify_ssl = False
        self._use_https = False


def _dedup_entry():
    e = type("E", (), {})()
    e.entry_id = "dd_entry"
    e.data = {"host": "10.18.176.64"}
    e.options = {}
    return e


def test_duplicate_channel_entities_disabled_by_default():
    """重复通道的每通道实体默认禁用（用户决策 D1）。"""
    from custom_components.hikvision_isapi_performance import binary_sensor as bs
    coord = _DedupCoord({"1"}, channels=[{"id": "1", "name": "摄像机12"}])
    ents = bs._entities_for_channel(coord, _dedup_entry(),
                                    {"id": "1", "name": "摄像机12"})
    assert ents, "应生成通道实体"
    for e in ents:
        assert e._attr_entity_registry_enabled_default is False, (
            f"{e._attr_unique_id} 应默认禁用")


def test_unique_channel_entities_enabled_by_default():
    """非重复通道（ch2 摄像机13）实体正常启用。"""
    from custom_components.hikvision_isapi_performance import binary_sensor as bs
    coord = _DedupCoord({"1"}, channels=[{"id": "2", "name": "摄像机13"}])
    ents = bs._entities_for_channel(coord, _dedup_entry(),
                                    {"id": "2", "name": "摄像机13"})
    for e in ents:
        assert e._attr_entity_registry_enabled_default is not False, (
            f"{e._attr_unique_id} 不应被禁用")


def test_per_channel_sensors_disabled_for_duplicates():
    """sensor 平台的每通道实体同样遵循默认禁用。"""
    from custom_components.hikvision_isapi_performance import sensor as sm
    coord = _DedupCoord({"3"}, channels=[{"id": "3", "name": "摄像机09"}])
    ents = sm._build_per_channel_entities(coord, _dedup_entry(),
                                          {"id": "3", "name": "摄像机09"})
    assert ents
    for e in ents:
        assert e.entity_description.entity_registry_enabled_default is False


def test_recording_time_sensors_disabled_for_duplicates():
    """录像时间实体同样禁用（重复通道的 NVR 视角实体）。"""
    from custom_components.hikvision_isapi_performance import sensor as sm
    coord = _DedupCoord({"1"})
    coord.recording_status = {"1": {
        "recording_active": True,
        "last_recording_time": None,
        "codec_type": None, "record_type": None,
    }}
    ents = sm._build_last_recording_time_entities(
        coord, _dedup_entry(), [{"id": "1", "name": "摄像机12"}])
    assert ents
    for e in ents:
        assert e.entity_description.entity_registry_enabled_default is False


def test_motion_switch_disabled_for_duplicates():
    """移动侦测开关同样禁用。"""
    from custom_components.hikvision_isapi_performance import switch as sw
    coord = _DedupCoord({"1"})
    coord.motion_detection = {"1": {"enabled": True, "sensitivity_level": 60}}
    ents = sw._build_motion_detection_entities(coord, _dedup_entry())
    assert ents
    assert ents[0]._attr_entity_registry_enabled_default is False


def test_device_level_entities_never_disabled():
    """设备级实体（HDD/存储/系统）不因通道去重被禁用。"""
    from custom_components.hikvision_isapi_performance import binary_sensor as bs
    coord = _DedupCoord({"1", "3", "4"})
    coord.storage = {"hdds": [{"id": "1", "name": "hdd1", "status": "ok"}]}
    ents = bs._build_per_hdd_binary_entities(coord, _dedup_entry())
    assert ents
    for e in ents:
        assert getattr(e, "_attr_entity_registry_enabled_default", True) is not False
