"""v0.8 步骤9：alertStream 事件二值传感器。

用户决策：每通道 3 个传感器（移动侦测 MOTION、视频丢失 PROBLEM、遮挡 TAMPER）
+ 事件总线派发（总线派发已在 test_v08_event_coordinator.py 覆盖）。

去重决策（关键）：现有 ``channel_{N}_motion`` 是轮询式实体。若再建一个
推送式 motion 实体，同一通道会有**两个运动传感器** —— 正是用户要求消除的
重复。因此：
  * motion：**复用现有实体**，改为优先读 event_state["motion"]，
    无推送数据时回退轮询的 motion_detected（保持 unique_id 稳定）
  * video_loss / tamper：**新建**（现无对应实体）

能力门控：只对设备**实际推送过**的 (事件类型, 通道) 建实体。
真机依据：
  * 176.64 推 VMD，6 个通道（1,2,3,10,11,12），channelID 为真实通道号
  * 176.65 / 192.168.10.17 推 videoloss，channelID=0（设备级，非通道级）
  * 176.12 推 VMD + videoloss（通道 1）
  * Event/triggers 目录**不含 videoloss**，但 alertStream 实际推送
    → 门控必须依据 alertStream 观测，不能依据 triggers 目录

实体是迟到数据（event_state 由读取器任务异步填充），必须配迟到监听器。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import binary_sensor as bs_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    HikvisionISAPIData,
)
from custom_components.hikvision_isapi_performance.const import DOMAIN  # noqa: E402


def _data(channels=None, recording_status=None) -> HikvisionISAPIData:
    return HikvisionISAPIData(
        device_info={"deviceType": "NVR"}, system_status={},
        channels=channels or [], capabilities={}, storage={},
        network_interfaces=[], streaming_channel_detail={},
        recording_status=recording_status or {},
    )


class _Coord:
    def __init__(self, data, *, event_state=None, event_types_seen=None):
        self.data = data
        self.channels = data.channels
        self.recording_status = data.recording_status
        self.storage: dict = {}
        self.network_interfaces: list = []
        self.device_type = "networkvideorecorder"
        self.device_info = data.device_info
        self.capabilities: dict = {}
        self.streaming_channel_detail: dict = {}
        self.system_capabilities: dict = {}
        self.system_status: dict = {}
        self.event_state = event_state if event_state is not None else {}
        self.event_types_seen = event_types_seen or set()
        self.unique_id = f"{DOMAIN}_evt"
        self._host = "10.18.176.64"
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


def _entry():
    e = type("E", (), {})()
    e.entry_id = "evt_entry"
    e.data = {"host": "10.18.176.64"}
    e.options = {}
    return e


# ── 1. 事件实体生成 ────────────────────────────────────────────────


def test_builds_video_loss_and_tamper_entities():
    """设备推送了 video_loss 与 tamper → 各建一个实体。"""
    coord = _Coord(_data(), event_state={
        "video_loss": {"1": False},
        "tamper": {"1": False},
    })
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    uids = [e._attr_unique_id for e in ents]
    assert any(u.endswith("channel_1_video_loss") for u in uids)
    assert any(u.endswith("channel_1_tamper") for u in uids)


def test_no_motion_entity_built_by_event_builder():
    """事件 builder **不得**建 motion 实体（避免与轮询实体重复）。

    去重要求：motion 复用现有 channel_{N}_motion 实体，
    由它优先读推送数据。此处再建一个就是重复实体。
    """
    coord = _Coord(_data(), event_state={"motion": {"1": True}})
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    uids = [e._attr_unique_id for e in ents]
    assert not any("motion" in u for u in uids), f"不得建 motion 实体: {uids}"


def test_no_entities_when_no_event_data():
    """无推送数据（未连接/设备不推）→ 不建任何事件实体。"""
    assert bs_mod._build_event_binary_entities(_Coord(_data()), _entry()) == []


def test_multiple_channels_get_own_entities():
    """真机 176.64 推 6 个通道 → 每通道各自实体。"""
    coord = _Coord(_data(), event_state={
        "video_loss": {"1": False, "2": False, "3": False,
                       "10": False, "11": False, "12": False},
    })
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    uids = {e._attr_unique_id for e in ents}
    for ch in ("1", "2", "3", "10", "11", "12"):
        assert any(u.endswith(f"channel_{ch}_video_loss") for u in uids), ch


def test_device_level_event_channel_zero_gets_entity():
    """channelID=0 的设备级事件（176.65/192.168.10.17 实测）也要建实体。"""
    coord = _Coord(_data(), event_state={"video_loss": {"0": False}})
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    assert len(ents) == 1
    assert ents[0]._attr_unique_id.endswith("channel_0_video_loss")


def test_entity_uses_channel_name_when_available():
    """有通道名时实体名带摄像机名（真机 alertStream 含 channelName）。"""
    coord = _Coord(
        _data(channels=[{"id": "3", "name": "摄像机09"}]),
        event_state={"video_loss": {"3": False}},
    )
    coord.event_meta = {"3": {"channel_name": "摄像机09"}}
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    assert "摄像机09" in ents[0]._attr_name


def test_entity_falls_back_to_generic_name():
    """无通道名时回退通用名，不得崩溃。"""
    coord = _Coord(_data(), event_state={"video_loss": {"7": False}})
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    assert ents[0]._attr_name  # 非空


# ── 2. 实体状态读取 ────────────────────────────────────────────────


def test_video_loss_sensor_reflects_state():
    coord = _Coord(_data(), event_state={"video_loss": {"1": True}})
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    assert ents[0].is_on is True


def test_video_loss_inactive_is_off():
    coord = _Coord(_data(), event_state={"video_loss": {"1": False}})
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    assert ents[0].is_on is False


def test_tamper_device_class():
    """遮挡用 TAMPER device class（HA 标准语义）。"""
    coord = _Coord(_data(), event_state={"tamper": {"1": True}})
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    assert ents[0]._attr_device_class == "tamper"


def test_video_loss_device_class_is_problem():
    """视频丢失用 PROBLEM device class（HA 渲染为红色告警）。"""
    coord = _Coord(_data(), event_state={"video_loss": {"1": True}})
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    assert ents[0]._attr_device_class == "problem"


def test_state_updates_after_coordinator_change():
    """coordinator.event_state 变化后实体读到新值（推送实时性）。"""
    coord = _Coord(_data(), event_state={"video_loss": {"1": False}})
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    assert ents[0].is_on is False
    coord.event_state["video_loss"]["1"] = True
    assert ents[0].is_on is True


def test_unknown_when_channel_absent():
    """该通道无推送数据 → None（unknown），不谎报 False。"""
    coord = _Coord(_data(), event_state={"video_loss": {"1": True}})
    e = bs_mod.HikvisionISAPIEventBinarySensor(
        coord, _entry(), "video_loss", "9", "ch9")
    assert e.is_on is None


# ── 3. motion 实体优先读推送数据（去重设计）───────────────────────


def test_motion_sensor_prefers_push_data():
    """现有 motion 实体优先读 event_state（推送比轮询快）。"""
    coord = _Coord(_data(channels=[{"id": "1", "name": "摄像机12",
                                   "motion_detected": False}]))
    coord.event_state = {"motion": {"1": True}}
    e = bs_mod.HikvisionISAPIChannelMotionBinarySensor(
        coord, _entry(), "1", "摄像机12")
    # 轮询说 False，推送说 True → 取推送（更新鲜）
    assert e.is_on is True


def test_motion_sensor_falls_back_to_polling():
    """无推送数据时回退轮询的 motion_detected（保持既有行为）。"""
    coord = _Coord(_data(channels=[{"id": "1", "name": "摄像机12",
                                   "motion_detected": True}]))
    e = bs_mod.HikvisionISAPIChannelMotionBinarySensor(
        coord, _entry(), "1", "摄像机12")
    assert e.is_on is True


def test_motion_sensor_unknown_when_neither_source():
    """两个源都无 → None（unknown）。"""
    coord = _Coord(_data(channels=[{"id": "1", "name": "x"}]))
    e = bs_mod.HikvisionISAPIChannelMotionBinarySensor(coord, _entry(), "1", "x")
    assert e.is_on is None


def test_motion_push_inactive_overrides_polling_true():
    """推送 inactive 优先于轮询 True（停报后不得一直显示有运动）。"""
    coord = _Coord(_data(channels=[{"id": "1", "name": "x",
                                   "motion_detected": True}]))
    coord.event_state = {"motion": {"1": False}}
    e = bs_mod.HikvisionISAPIChannelMotionBinarySensor(coord, _entry(), "1", "x")
    assert e.is_on is False


# ── 4. 接线：驱动真实 async_setup_entry ────────────────────────────


class _FakeCoord(_Coord):
    """带 HA 式监听器语义的假 coordinator，用于接线测试。"""

    def __init__(self, **kw):
        super().__init__(_data(), **kw)
        self.device_type = ""


def _hass(coord):
    h = type("H", (), {})()
    h.data = {DOMAIN: {"evt_entry": coord}}
    return h


@pytest.mark.asyncio
async def test_event_entities_registered_when_events_arrive_late():
    """事件迟到到达 → 事件实体被注册（推送连接建立后才有数据）。"""
    coord = _FakeCoord()
    added: list = []
    await bs_mod.async_setup_entry(_hass(coord), _entry(), added.extend)

    offenders = [getattr(f, "__name__", repr(f)) for f in coord._listeners
                 if inspect.iscoroutinefunction(f)]
    assert not offenders, f"async def 监听器函数体永不执行: {offenders}"

    # 推送连接建立，事件到达
    coord.event_state = {"video_loss": {"1": False}, "tamper": {"1": False}}
    coord.fire()

    assert not coord.unawaited_coroutines, "监听器返回协程 → 函数体未执行"
    uids = {e._attr_unique_id for e in added if hasattr(e, "_attr_unique_id")}
    assert any(u.endswith("channel_1_video_loss") for u in uids)
    assert any(u.endswith("channel_1_tamper") for u in uids)


@pytest.mark.asyncio
async def test_event_entities_not_double_registered():
    """多次刷新不得重复注册同一事件实体。"""
    coord = _FakeCoord()
    added: list = []
    await bs_mod.async_setup_entry(_hass(coord), _entry(), added.extend)

    coord.event_state = {"video_loss": {"1": False}}
    coord.fire()
    coord.fire()
    coord.fire()

    n = sum(1 for e in added
            if getattr(e, "_attr_unique_id", "").endswith("channel_1_video_loss"))
    assert n == 1, f"重复注册: {n}"


@pytest.mark.asyncio
async def test_new_channel_events_registered_incrementally():
    """后续新通道开始推送 → 增量注册该通道实体。"""
    coord = _FakeCoord()
    added: list = []
    await bs_mod.async_setup_entry(_hass(coord), _entry(), added.extend)

    coord.event_state = {"video_loss": {"1": False}}
    coord.fire()
    # 通道 2 稍后才开始推送
    coord.event_state["video_loss"]["2"] = False
    coord.fire()

    uids = {e._attr_unique_id for e in added if hasattr(e, "_attr_unique_id")}
    assert any(u.endswith("channel_1_video_loss") for u in uids)
    assert any(u.endswith("channel_2_video_loss") for u in uids)


@pytest.mark.asyncio
async def test_no_event_entities_without_push_data():
    """设备不推送（或连接失败）→ 不建事件实体，轮询实体照常工作。"""
    coord = _FakeCoord()
    added: list = []
    await bs_mod.async_setup_entry(_hass(coord), _entry(), added.extend)
    coord.fire()
    coord.fire()

    uids = [getattr(e, "_attr_unique_id", "") for e in added]
    assert not any("video_loss" in u for u in uids)
    assert not any("_tamper" in u for u in uids)


def test_triggers_catalog_is_not_used_for_gating():
    """门控依据 alertStream 观测，不得依据 Event/triggers 目录。

    真机实测：4 台设备的 triggers 目录都**不含 videoloss**，但
    alertStream 实际推送 videoloss。若用目录门控，video_loss 实体
    将在所有设备上都不被创建。
    """
    coord = _Coord(_data())
    coord.event_types_seen = {"videoloss"}  # 只从 alertStream 观测到
    coord.event_state = {"video_loss": {"1": False}}
    ents = bs_mod._build_event_binary_entities(coord, _entry())
    assert ents, "仅凭 alertStream 观测就应建实体，无需 triggers 目录支持"
