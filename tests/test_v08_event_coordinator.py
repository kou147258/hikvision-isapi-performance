"""v0.8 步骤9a：coordinator 事件回调与生命周期接线。

_on_stream_event 把 alertStream 事件写入 coordinator.event_state，
供事件二值传感器消费，并向 HA 事件总线派发。这段逻辑决定传感器状态
是否正确，必须测试。

关键契约（真机数据驱动）：
  * VMD → motion、videoloss → video_loss、tamperdetection/tamper → tamper
  * eventState=active → True，inactive → False
  * channelID=0 是设备级事件（176.65 / 192.168.10.17 实测如此）
  * 只在状态**真正变化**时唤醒监听器 —— 176.10 是消防水管
    （8 秒 2.25MB），无脑唤醒会让实体状态被反复刷新
  * 未建模的事件类型（diskfull/ipconflict 等）不建传感器键，
    但仍派发事件总线供自动化使用
  * 事件总线 fire 失败不得影响状态更新（best-effort）

零真实网络：hass 与 reader 全部用假对象。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import coordinator as coord_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.const import DOMAIN  # noqa: E402
from custom_components.hikvision_isapi_performance.event_stream import Event  # noqa: E402


class _FakeBus:
    def __init__(self, *, raise_exc: bool = False):
        self.fired: list[tuple[str, dict]] = []
        self._raise = raise_exc

    def async_fire(self, event_type: str, data: dict) -> None:
        if self._raise:
            raise RuntimeError("bus broken")
        self.fired.append((event_type, data))


class _FakeHass:
    def __init__(self, *, bus_raise: bool = False):
        self.bus = _FakeBus(raise_exc=bus_raise)
        self.tasks: list[tuple[Any, str]] = []

    def async_create_task(self, coro, name=None):
        # 记录但不真正运行（生命周期测试单独驱动）。
        # 必须 close() 掉协程，否则会留下
        # "coroutine was never awaited" 的 RuntimeWarning。
        self.tasks.append((coro, name))
        coro.close()
        return type("T", (), {"done": lambda self: False})()


def _make_coordinator(*, bus_raise: bool = False):
    """构造最小 coordinator（绕过 __init__ 的 DataUpdateCoordinator 依赖）。"""
    c = coord_mod.HikvisionISAPICoordinator.__new__(
        coord_mod.HikvisionISAPICoordinator)
    c._host = "10.18.176.64"
    c._port = 80
    c._username = "admin"
    c._password = "pw"
    c._verify_ssl = False
    c._use_https = False
    c.hass = _FakeHass(bus_raise=bus_raise)
    c.event_state = {}
    c.event_meta = {}
    c.event_types_seen = set()
    c._event_reader = None
    c._event_task = None
    c.listener_wakeups = 0

    def _wake():
        c.listener_wakeups += 1

    c.async_update_listeners = _wake
    return c


def _ev(channel: str, et: str, state: str, name: str | None = None) -> Event:
    return Event(
        channel_id=channel, event_type=et, event_state=state,
        channel_name=name, dyn_channel_id=channel,
        date_time="2026-09-27T08:25:13+08:00", active_post_count=1,
    )


# ── 1. 事件类型 → 传感器键映射 ────────────────────────────────────


def test_vmd_maps_to_motion_sensor_key():
    """VMD（真机所有推送移动侦测的设备用的类型）→ motion。"""
    c = _make_coordinator()
    c._on_stream_event(_ev("1", "VMD", "active", "摄像机12"))
    assert c.event_state["motion"]["1"] is True


def test_videoloss_maps_to_video_loss_key():
    """videoloss → video_loss（实测 10/11 台推送此类型）。"""
    c = _make_coordinator()
    c._on_stream_event(_ev("1", "videoloss", "inactive"))
    assert c.event_state["video_loss"]["1"] is False


def test_tamper_maps_to_tamper_key():
    """tamperdetection 与 tamper 两种拼写都 → tamper。"""
    for et in ("tamperdetection", "tamper"):
        c = _make_coordinator()
        c._on_stream_event(_ev("1", et, "active"))
        assert "tamper" in c.event_state, f"{et} 未映射到 tamper"
        assert c.event_state["tamper"]["1"] is True


def test_unmodelled_event_type_creates_no_sensor_key():
    """未建模类型（diskfull/ipconflict）不产生传感器键。

    真机 Event/triggers 目录显示设备会报 diskfull、ipconflict、
    nicbroken、illaccess 等 15+ 种类型；为每种建实体是噪音。
    """
    c = _make_coordinator()
    for et in ("diskfull", "ipconflict", "nicbroken", "illaccess"):
        c._on_stream_event(_ev("1", et, "active"))
    assert c.event_state == {}, f"不应产生传感器键: {c.event_state}"
    # 但仍记录到 event_types_seen 供诊断
    assert c.event_types_seen == {"diskfull", "ipconflict", "nicbroken", "illaccess"}


def test_unmodelled_event_still_fires_on_bus():
    """未建模事件仍派发事件总线（自动化可用）。"""
    c = _make_coordinator()
    c._on_stream_event(_ev("1", "diskfull", "active"))
    assert len(c.hass.bus.fired) == 1
    assert c.hass.bus.fired[0][1]["event_type"] == "diskfull"


# ── 2. 状态语义 ────────────────────────────────────────────────────


def test_active_and_inactive_states():
    c = _make_coordinator()
    c._on_stream_event(_ev("1", "VMD", "active"))
    assert c.event_state["motion"]["1"] is True
    c._on_stream_event(_ev("1", "VMD", "inactive"))
    assert c.event_state["motion"]["1"] is False


def test_active_state_case_insensitive():
    """eventState 大小写不敏感（固件差异）。"""
    c = _make_coordinator()
    c._on_stream_event(_ev("1", "VMD", "ACTIVE"))
    assert c.event_state["motion"]["1"] is True
    c._on_stream_event(_ev("1", "VMD", "Inactive"))
    assert c.event_state["motion"]["1"] is False


def test_multiple_channels_tracked_independently():
    """真机 176.64 推送 6 个通道 → 各自独立跟踪。"""
    c = _make_coordinator()
    for ch in ("1", "2", "3", "10", "11", "12"):
        c._on_stream_event(_ev(ch, "VMD", "active"))
    assert c.event_state["motion"] == {
        "1": True, "2": True, "3": True, "10": True, "11": True, "12": True}


def test_device_level_event_channel_zero():
    """channelID=0 的设备级事件（176.65/192.168.10.17 实测）。"""
    c = _make_coordinator()
    c._on_stream_event(_ev("0", "videoloss", "inactive"))
    assert c.event_state["video_loss"]["0"] is False


def test_missing_channel_id_falls_back_to_zero():
    """无 channelID 的事件归到 "0"，不丢弃。"""
    c = _make_coordinator()
    c._on_stream_event(Event(channel_id="", event_type="VMD", event_state="active"))
    assert c.event_state["motion"]["0"] is True


# ── 3. 唤醒节流（消防水管防护）────────────────────────────────────


def test_wakes_listeners_only_on_state_change():
    """重复的相同状态不得反复唤醒监听器。

    176.10 实测 8 秒推 2.25MB；若每次都唤醒，实体状态会被无意义刷新。
    """
    c = _make_coordinator()
    c._on_stream_event(_ev("1", "VMD", "active"))
    first = c.listener_wakeups
    for _ in range(50):
        c._on_stream_event(_ev("1", "VMD", "active"))
    assert c.listener_wakeups == first, "相同状态重复事件不应唤醒"


def test_wakes_on_genuine_transition():
    """真实状态转换必须唤醒。"""
    c = _make_coordinator()
    c._on_stream_event(_ev("1", "VMD", "active"))
    before = c.listener_wakeups
    c._on_stream_event(_ev("1", "VMD", "inactive"))
    assert c.listener_wakeups == before + 1


def test_wakes_on_active_inactive_active_sequence():
    """active→inactive→active 三次都必须生效。

    这是删掉全局 dedupe() 的原因：全局去抖会吞掉第三次事件，
    导致运动传感器在恢复后永久失效。
    """
    c = _make_coordinator()
    c._on_stream_event(_ev("1", "VMD", "active"))
    c._on_stream_event(_ev("1", "VMD", "inactive"))
    c._on_stream_event(_ev("1", "VMD", "active"))
    assert c.event_state["motion"]["1"] is True
    assert c.listener_wakeups == 3


def test_different_channels_wake_independently():
    c = _make_coordinator()
    c._on_stream_event(_ev("1", "VMD", "active"))
    before = c.listener_wakeups
    c._on_stream_event(_ev("2", "VMD", "active"))
    assert c.listener_wakeups == before + 1


# ── 4. 事件总线 ────────────────────────────────────────────────────


def test_event_bus_payload_fields():
    """派发的事件负载含自动化所需的字段。"""
    c = _make_coordinator()
    c._on_stream_event(_ev("3", "VMD", "active", "摄像机09"))
    et, data = c.hass.bus.fired[0]
    assert et == f"{DOMAIN}_event"
    assert data["channel_id"] == "3"
    assert data["channel_name"] == "摄像机09"
    assert data["event_type"] == "VMD"
    assert data["event_state"] == "active"
    assert data["host"] == "10.18.176.64"
    assert data["date_time"] == "2026-09-27T08:25:13+08:00"
    assert data["active_post_count"] == 1


def test_event_bus_failure_does_not_break_state_update():
    """事件总线抛异常不得影响状态更新（best-effort）。"""
    c = _make_coordinator(bus_raise=True)
    c._on_stream_event(_ev("1", "VMD", "active"))
    # 状态仍正确写入
    assert c.event_state["motion"]["1"] is True


def test_event_meta_records_last_event_per_channel():
    c = _make_coordinator()
    c._on_stream_event(_ev("3", "VMD", "active", "摄像机09"))
    assert c.event_meta["3"]["channel_name"] == "摄像机09"
    assert c.event_meta["3"]["event_type"] == "VMD"


# ── 5. 生命周期 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_stops_reader():
    """async_shutdown 必须停止读取器（否则卸载集成后连接泄漏）。"""
    c = _make_coordinator()
    stopped = []

    class _FakeReader:
        async def stop(self, task):
            stopped.append(task)

    c._event_reader = _FakeReader()
    c._event_task = object()
    await c.async_shutdown()
    assert stopped == [c._event_task] or len(stopped) == 1
    assert c._event_reader is None
    assert c._event_task is None


@pytest.mark.asyncio
async def test_shutdown_tolerates_stop_failure():
    """stop() 抛异常时 shutdown 仍完成（不得让卸载失败）。"""
    c = _make_coordinator()

    class _BadReader:
        async def stop(self, task):
            raise RuntimeError("stop failed")

    c._event_reader = _BadReader()
    c._event_task = object()
    await c.async_shutdown()  # 不应抛异常
    assert c._event_reader is None


@pytest.mark.asyncio
async def test_shutdown_without_reader_is_noop():
    """从未启动事件流时 shutdown 不崩溃。"""
    c = _make_coordinator()
    await c.async_shutdown()


def test_start_event_stream_is_idempotent():
    """重复调用不得创建多个读取器任务。"""
    c = _make_coordinator()

    class _FakeTask:
        def done(self):
            return False

    # 第一次：创建
    c.async_start_event_stream()
    assert c._event_reader is not None
    first_task = c._event_task
    # 第二次：应跳过
    c._event_task = _FakeTask()
    c.async_start_event_stream()
    assert c._event_task is not None


# ── 6. 卸载生命周期：读取器不得泄漏 ────────────────────────────────
#
# 缺陷：__init__.async_unload_entry 只卸载平台、注销 PTZ 服务、弹出
# hass.data，但从不触发 coordinator 的关闭。而 super().__init__ 未传
# config_entry，HA 也不会自动调用 async_shutdown。结果：卸载集成后
# alertStream 长连接与读取器任务永久存活（连接泄漏 + 幽灵任务）。


@pytest.mark.asyncio
async def test_unload_entry_stops_event_reader():
    """async_unload_entry 必须停掉 alertStream 读取器。"""
    import custom_components.hikvision_isapi_performance as init_mod

    c = _make_coordinator()
    stopped = []

    class _FakeReader:
        async def stop(self, task):
            stopped.append(task)

    c._event_reader = _FakeReader()
    c._event_task = object()

    calls = {"unloaded": False, "unregistered": False}

    class _FakeConfigEntries:
        async def async_unload_platforms(self, entry, platforms):
            calls["unloaded"] = True
            return True

    hass = _FakeHass()
    hass.config_entries = _FakeConfigEntries()
    hass.data = {DOMAIN: {"evt_entry": c}}

    # 替换 PTZ 注销，避免依赖真实 hass.services
    orig = init_mod.async_unregister_ptz_service

    async def _noop_unregister(h, e=None):
        calls["unregistered"] = True

    init_mod.async_unregister_ptz_service = _noop_unregister
    try:
        entry = type("E", (), {})()
        entry.entry_id = "evt_entry"
        entry.data = {}
        ok = await init_mod.async_unload_entry(hass, entry)
    finally:
        init_mod.async_unregister_ptz_service = orig

    assert ok is True
    assert calls["unloaded"], "应卸载平台"
    assert len(stopped) == 1, f"读取器必须被停止，实停 {len(stopped)} 次"


@pytest.mark.asyncio
async def test_unload_entry_removes_coordinator_from_data():
    """卸载后 hass.data[DOMAIN] 不再保留该 entry 的 coordinator。"""
    import custom_components.hikvision_isapi_performance as init_mod

    c = _make_coordinator()

    class _FakeConfigEntries:
        async def async_unload_platforms(self, entry, platforms):
            return True

    hass = _FakeHass()
    hass.config_entries = _FakeConfigEntries()
    hass.data = {DOMAIN: {"evt_entry": c}}

    orig = init_mod.async_unregister_ptz_service

    async def _noop(h, e=None):
        return None

    init_mod.async_unregister_ptz_service = _noop
    try:
        entry = type("E", (), {})()
        entry.entry_id = "evt_entry"
        entry.data = {}
        await init_mod.async_unload_entry(hass, entry)
    finally:
        init_mod.async_unregister_ptz_service = orig

    assert "evt_entry" not in hass.data[DOMAIN]
