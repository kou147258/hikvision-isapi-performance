"""v0.8 步骤6：录像活动实体（用户症状 #1 的解法）。

现有 HikvisionISAPIChannelRecordingBinarySensor 读 ch.recording，而
NVR 的该字段恒为 None（InputProxy/Recording 端点都无录像状态）→ 永远
显示 unknown，这正是用户抱怨的"录像中显示未在运行/未知"。

v0.8 新增 recording_status（来自 /ContentMgmt/search 录像片段推导）。
本步让实体消费它：
  * 录像二值传感器：优先读 recording_status[ch].recording_active，
    回退 ch.recording（某些 IPC 的 status 端点确报录像态）
  * 新增「最近录像时间」传感器（TIMESTAMP）：仅对 recording_status
    有数据的通道创建（能力门控，避免永久 unknown 实体）

实体名标注"推导"，诚实反映这是从片段计划时间推出、非设备直报。
测试驱动真实实体类，不联网。
"""

from __future__ import annotations

import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import binary_sensor as bs_mod  # noqa: E402
from custom_components.hikvision_isapi_performance import sensor as sensor_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    HikvisionISAPIData,
)
from custom_components.hikvision_isapi_performance.const import DOMAIN  # noqa: E402


def _data(recording_status=None, channels=None) -> HikvisionISAPIData:
    return HikvisionISAPIData(
        device_info={"deviceType": "NVR"}, system_status={},
        channels=channels or [], capabilities={}, storage={},
        network_interfaces=[], streaming_channel_detail={},
        recording_status=recording_status or {},
    )


class _Coord:
    def __init__(self, data):
        self.data = data
        self.channels = data.channels
        self.network_interfaces = []
        self.device_type = "networkvideorecorder"
        self.device_info = data.device_info
        self.capabilities = {}
        self.streaming_channel_detail = {}
        self.storage = data.storage
        self.recording_status = data.recording_status
        self.unique_id = f"{DOMAIN}_rec"
        self._host = "192.168.10.17"
        self._port = 80
        self._username = "admin"
        self._password = "pw"
        self._verify_ssl = False
        self._use_https = False

    def async_add_listener(self, listener):
        return lambda: None


def _entry():
    e = type("E", (), {})()
    e.entry_id = "rec_entry"
    e.data = {"host": "192.168.10.17"}
    e.options = {}
    return e


def _rec_status(active: bool, last: str = "2026-09-27T01:26:41Z") -> dict:
    return {
        "recording_active": active,
        "last_recording_time": datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc),
        "codec_type": "H.264-BP",
        "record_type": "timing",
    }


# ── 录像二值传感器：优先读 recording_status ────────────────────────


def test_recording_sensor_reads_derived_active_true():
    """recording_status 说在录 → is_on=True（不再是 unknown）。"""
    data = _data(recording_status={"1": _rec_status(True)})
    e = bs_mod.HikvisionISAPIChannelRecordingBinarySensor(
        _Coord(data), _entry(), "1", "摄像机04")
    assert e.is_on is True


def test_recording_sensor_reads_derived_active_false():
    """recording_status 说未在录（停录超过容忍窗口）→ is_on=False。"""
    data = _data(recording_status={"1": _rec_status(False)})
    e = bs_mod.HikvisionISAPIChannelRecordingBinarySensor(
        _Coord(data), _entry(), "1", "摄像机04")
    assert e.is_on is False


def test_recording_sensor_falls_back_to_channel_recording():
    """无 recording_status 时回退 ch.recording（某些 IPC 直报录像态）。"""
    data = _data(channels=[{"id": "1", "name": "x", "recording": True}])
    e = bs_mod.HikvisionISAPIChannelRecordingBinarySensor(
        _Coord(data), _entry(), "1", "x")
    assert e.is_on is True


def test_recording_sensor_derived_beats_channel_recording():
    """recording_status 优先于 ch.recording（片段推导更新鲜）。"""
    data = _data(
        recording_status={"1": _rec_status(False)},
        channels=[{"id": "1", "name": "x", "recording": True}],
    )
    e = bs_mod.HikvisionISAPIChannelRecordingBinarySensor(
        _Coord(data), _entry(), "1", "x")
    # recording_status 说 False，即使 ch.recording 说 True
    assert e.is_on is False


def test_recording_sensor_unknown_when_neither_source():
    """两个源都无数据 → None（unknown），不谎报 False。"""
    data = _data(channels=[{"id": "1", "name": "x", "recording": None}])
    e = bs_mod.HikvisionISAPIChannelRecordingBinarySensor(
        _Coord(data), _entry(), "1", "x")
    assert e.is_on is None


def test_recording_sensor_channel_absent_returns_none():
    """该通道在两个源都不存在 → None。"""
    data = _data(recording_status={"2": _rec_status(True)})
    e = bs_mod.HikvisionISAPIChannelRecordingBinarySensor(
        _Coord(data), _entry(), "1", "x")
    assert e.is_on is None


def test_recording_sensor_name_marks_derived():
    """实体名标注"推导"，诚实反映非设备直报。"""
    data = _data(recording_status={"1": _rec_status(True)})
    e = bs_mod.HikvisionISAPIChannelRecordingBinarySensor(
        _Coord(data), _entry(), "1", "摄像机04")
    assert "推导" in e._attr_name or "推算" in e._attr_name


# ── 最近录像时间传感器 ────────────────────────────────────────────


def test_last_recording_time_sensor_value():
    """最近录像时间 = recording_status 的 last_recording_time。"""
    data = _data(recording_status={"1": _rec_status(True, "2026-09-27T01:26:41Z")})
    ents = sensor_mod._build_last_recording_time_entities(_Coord(data), _entry(),
                                                      [{"id": "1", "name": "摄像机04"}])
    assert ents
    e = ents[0]
    assert e.native_value == datetime(2026, 9, 27, 1, 26, 41, tzinfo=timezone.utc)


def test_last_recording_time_only_for_channels_with_data():
    """只对 recording_status 有数据的通道建实体（能力门控）。"""
    data = _data(recording_status={"1": _rec_status(True)})
    channels = [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]
    ents = sensor_mod._build_last_recording_time_entities(_Coord(data), _entry(), channels)
    uids = [e._attr_unique_id for e in ents]
    assert any("channel_1_" in u for u in uids)
    assert not any("channel_2_" in u for u in uids), "通道2无录像数据不应建实体"


def test_last_recording_time_no_data_no_entities():
    """recording_status 为空（IPC/无录像）→ 不建任何实体。"""
    data = _data(recording_status={})
    ents = sensor_mod._build_last_recording_time_entities(
        _Coord(data), _entry(), [{"id": "1", "name": "a"}])
    assert ents == []


def test_last_recording_time_is_timestamp_diagnostic():
    """device_class=TIMESTAMP + 诊断分类。

    sensor 平台走描述式：属性由 entity_description 承载（真实 HA 的
    SensorEntity.__init__ 会把它映射到 _attr_*，桩不做这层映射）。
    """
    data = _data(recording_status={"1": _rec_status(True)})
    ents = sensor_mod._build_last_recording_time_entities(_Coord(data), _entry(),
                                                      [{"id": "1", "name": "a"}])
    desc = ents[0].entity_description
    assert desc.device_class == "timestamp"
    assert desc.entity_category == "diagnostic"


def test_last_recording_time_unique_id():
    data = _data(recording_status={"1": _rec_status(True)})
    ents = sensor_mod._build_last_recording_time_entities(_Coord(data), _entry(),
                                                      [{"id": "1", "name": "a"}])
    assert "channel_1_last_recording" in ents[0]._attr_unique_id


def test_last_recording_time_carries_channel_name():
    """实体名带摄像机名（描述式：name 在 entity_description 上）。"""
    data = _data(recording_status={"1": _rec_status(True)})
    ents = sensor_mod._build_last_recording_time_entities(_Coord(data), _entry(),
                                                      [{"id": "1", "name": "摄像机05"}])
    assert "摄像机05" in ents[0].entity_description.name


def test_last_recording_time_has_no_translation_key():
    """动态实体必须 translation_key=None，否则所有通道渲染成同一标签。"""
    data = _data(recording_status={"1": _rec_status(True), "2": _rec_status(True)})
    ents = sensor_mod._build_last_recording_time_entities(
        _Coord(data), _entry(),
        [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}])
    assert len(ents) == 2
    for e in ents:
        assert e.entity_description.translation_key is None


# ── 接线测试：驱动真实 async_setup_entry ────────────────────────────
#
# 生成器存在 ≠ 实体出现在 HA。recording_status 与 channels 在同一次刷新
# 到达，但 per-channel 监听器有 already_ids 门闩（通道注册后就不再触发），
# 因此录像时间实体必须有**独立**的一次性监听器，否则会被门闩跳过而永不注册。
# 这正是 v0.7.3 死监听器缺陷的同类形态。


class _FakeCoord:
    """忠实模拟 HA 监听器语义：同步调用、丢弃返回值、记录协程。"""

    def __init__(self, *, channels=None, recording_status=None, device_type=""):
        self.channels = channels or []
        self.recording_status = recording_status or {}
        self.device_type = device_type
        self.network_interfaces: list = []
        self.device_info: dict = {}
        self.storage: dict = {}
        self.capabilities: dict = {}
        self.streaming_channel_detail: dict = {}
        self.system_capabilities: dict = {}
        self.system_status: dict = {}
        self.data = None
        self.unique_id = f"{DOMAIN}_recwire"
        self._host = "192.168.10.17"
        self._port = 80
        self._username = "admin"
        self._password = "pw"
        self._verify_ssl = False
        self._use_https = False
        self._listeners: list = []
        self.unawaited_coroutines: list = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: (
            self._listeners.remove(listener) if listener in self._listeners else None
        )

    def fire(self):
        for listener in list(self._listeners):
            result = listener()
            if hasattr(result, "__await__") or type(result).__name__ == "coroutine":
                self.unawaited_coroutines.append(result)
                result.close()


def _wire_hass(coord):
    h = type("H", (), {})()
    h.data = {DOMAIN: {"recwire": coord}}
    return h


def _wire_entry():
    e = type("E", (), {})()
    e.entry_id = "recwire"
    e.data = {"host": "192.168.10.17"}
    e.options = {}
    return e


@pytest.mark.asyncio
async def test_recording_time_registered_when_data_arrives_late():
    """recording_status 迟到到达 → 录像时间传感器被注册。"""
    coord = _FakeCoord(device_type="", channels=[], recording_status={})
    added: list = []
    await sensor_mod.async_setup_entry(_wire_hass(coord), _wire_entry(), added.extend)

    offenders = [getattr(f, "__name__", repr(f)) for f in coord._listeners
                 if inspect.iscoroutinefunction(f)]
    assert not offenders, f"async def 监听器函数体永不执行: {offenders}"

    # 首次刷新：通道与录像状态同时到达
    coord.channels = [{"id": "1", "name": "摄像机04"},
                      {"id": "2", "name": "摄像机05"}]
    coord.recording_status = {"1": _rec_status(True), "2": _rec_status(True)}
    coord.data = _data(coord.recording_status, coord.channels)
    coord.fire()

    assert not coord.unawaited_coroutines, "监听器返回协程 → 函数体未执行"
    keys = {e.entity_description.key for e in added
            if getattr(e, "entity_description", None)}
    assert "channel_1_last_recording" in keys
    assert "channel_2_last_recording" in keys


@pytest.mark.asyncio
async def test_recording_binary_sensor_reflects_derived_state_after_wiring():
    """接线后录像二值传感器读到的确实是推导状态（症状 #1 的验收）。"""
    coord = _FakeCoord(
        device_type="networkvideorecorder",
        channels=[{"id": "1", "name": "摄像机04", "recording": None}],
        recording_status={"1": _rec_status(True)},
    )
    coord.data = _data(coord.recording_status, coord.channels)
    added: list = []
    await bs_mod.async_setup_entry(_wire_hass(coord), _wire_entry(), added.extend)

    rec = [e for e in added
           if getattr(e, "_attr_unique_id", "").endswith("channel_1_recording")]
    assert rec, "录像二值传感器应注册"
    # 关键：不再是 None(unknown)，而是推导出的 True
    assert rec[0].is_on is True


@pytest.mark.asyncio
async def test_no_recording_entities_when_no_recording_data():
    """recording_status 为空（IPC/无录像/端点 403）→ 不建录像时间实体。"""
    coord = _FakeCoord(device_type="ipcamera",
                       channels=[{"id": "1", "name": "a", "recording": None}],
                       recording_status={})
    coord.data = _data({}, coord.channels)
    added: list = []
    await sensor_mod.async_setup_entry(_wire_hass(coord), _wire_entry(), added.extend)
    coord.fire()

    keys = {e.entity_description.key for e in added
            if getattr(e, "entity_description", None)}
    assert not any("last_recording" in k for k in keys), "无录像数据不应建录像时间实体"


@pytest.mark.asyncio
async def test_recording_time_not_double_registered():
    """多次刷新只注册一次录像时间实体。"""
    coord = _FakeCoord(device_type="")
    added: list = []
    await sensor_mod.async_setup_entry(_wire_hass(coord), _wire_entry(), added.extend)

    coord.channels = [{"id": "1", "name": "a"}]
    coord.recording_status = {"1": _rec_status(True)}
    coord.data = _data(coord.recording_status, coord.channels)
    coord.fire()
    coord.fire()
    coord.fire()

    n = sum(1 for e in added
            if getattr(e, "entity_description", None)
            and e.entity_description.key == "channel_1_last_recording")
    assert n == 1, f"重复刷新不得重复注册，实得 {n} 个"
