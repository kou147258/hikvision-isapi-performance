"""v0.8 步骤7：reboot_count 按能力创建（消除永久 unknown 实体）。

用户症状："NVR 的重启次数还是显示未知"。

实测根因：`totalRebootCount` 只有 6/12 台设备返回（176.10/12/13/51/52/53），
其余 6 台（含全部 3 台 NVR）的 /System/status **根本没有该字段**。
现有代码把 reboot_count 作为静态 SENSORS 项无条件注册，于是在那 6 台上
永远显示 unknown —— 这正是用户抱怨的噪音。

修复契约（用户已确认"按能力创建，不支持就不建"）：
  * 设备 system_status 含 rebootCount（值可以是 0）→ 注册 reboot_count 实体
  * 设备 system_status 无 rebootCount → **不注册**该实体
  * reboot_count 是迟到数据（setup 时 system_status 为空）→ 走迟到监听器
  * 值为 0 与字段缺失必须区分：0 是真实数据要建，缺失不建

测试驱动真实 async_setup_entry，确认实体确实注册/不注册（而非只测生成器）。
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

from custom_components.hikvision_isapi_performance import sensor as sensor_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    HikvisionISAPIData,
)
from custom_components.hikvision_isapi_performance.const import DOMAIN  # noqa: E402


class _FakeCoordinator:
    """忠实模拟监听器语义：fire() 同步调用、丢弃返回值、记录协程。"""

    def __init__(self, *, device_type="", system_status=None, data=None):
        self.device_type = device_type
        self.channels: list[dict] = []
        self.network_interfaces: list[dict] = []
        self.device_info: dict = {}
        self.system_status = system_status or {}
        self.storage: dict = {}
        self.data = data
        self.capabilities: dict = {}
        self.streaming_channel_detail: dict = {}
        self.system_capabilities: dict = {}
        self.unique_id = f"{DOMAIN}_cap"
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
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def fire(self):
        for listener in list(self._listeners):
            result = listener()
            if hasattr(result, "__await__") or type(result).__name__ == "coroutine":
                self.unawaited_coroutines.append(result)
                result.close()


def _hass(coord):
    h = type("H", (), {})()
    h.data = {DOMAIN: {"cap_entry": coord}}
    return h


def _entry():
    e = type("E", (), {})()
    e.entry_id = "cap_entry"
    e.data = {"host": "10.18.176.64"}
    e.options = {}
    return e


def _reboot_keys(added):
    return [e for e in added
            if getattr(e, "entity_description", None)
            and e.entity_description.key == "reboot_count"]


# ── 设备有 rebootCount → 建实体 ───────────────────────────────────


@pytest.mark.asyncio
async def test_reboot_count_registered_when_device_reports_it():
    """176.10（totalRebootCount=41）：刷新后 reboot_count 实体应出现。"""
    coord = _FakeCoordinator(device_type="")
    added: list = []
    await sensor_mod.async_setup_entry(_hass(coord), _entry(), added.extend)

    # setup 时 system_status 空 → 尚未注册
    assert not _reboot_keys(added)

    # 首次刷新带来 rebootCount=41
    coord.device_type = "ipcamera"
    coord.system_status = {"uptime": "172912", "rebootCount": "41"}
    coord.data = HikvisionISAPIData(
        device_info={"deviceType": "IPDome"},
        system_status=coord.system_status, channels=[], capabilities={},
        storage={}, network_interfaces=[], streaming_channel_detail={},
    )
    coord.fire()

    assert not coord.unawaited_coroutines, "监听器返回协程 → 函数体未执行"
    assert _reboot_keys(added), "有 rebootCount 的设备应注册 reboot_count 实体"


@pytest.mark.asyncio
async def test_reboot_count_registered_when_value_is_zero():
    """rebootCount=0 是真实数据（"重启过 0 次"），必须建实体。

    与"字段缺失"区分是核心：0 要建，缺失不建。
    """
    coord = _FakeCoordinator(device_type="ipcamera")
    coord.system_status = {"uptime": "100", "rebootCount": "0"}
    coord.data = HikvisionISAPIData(
        device_info={}, system_status=coord.system_status, channels=[],
        capabilities={}, storage={}, network_interfaces=[],
        streaming_channel_detail={},
    )
    added: list = []
    await sensor_mod.async_setup_entry(_hass(coord), _entry(), added.extend)
    # 同步路径已有数据 → 立即注册
    coord.fire()
    assert _reboot_keys(added), "rebootCount=0 应注册实体"


# ── 设备无 rebootCount → 不建实体 ─────────────────────────────────


@pytest.mark.asyncio
async def test_reboot_count_not_registered_when_field_absent():
    """NVR（176.64 无 totalRebootCount）：不得注册 reboot_count 实体。

    这是用户抱怨的核心 —— 消除永久 unknown 噪音。
    """
    coord = _FakeCoordinator(device_type="")
    added: list = []
    await sensor_mod.async_setup_entry(_hass(coord), _entry(), added.extend)

    # 首次刷新：有 uptime 但无 rebootCount（NVR 的真实形态）
    coord.device_type = "networkvideorecorder"
    coord.system_status = {"uptime": "315698", "cpuUtilization": "5"}
    coord.data = HikvisionISAPIData(
        device_info={"deviceType": "NVR"}, system_status=coord.system_status,
        channels=[], capabilities={}, storage={}, network_interfaces=[],
        streaming_channel_detail={},
    )
    coord.fire()

    assert not coord.unawaited_coroutines
    assert not _reboot_keys(added), (
        "无 rebootCount 字段的设备不应注册 reboot_count（否则永久 unknown）"
    )


@pytest.mark.asyncio
async def test_reboot_count_listener_is_sync_not_async():
    """监听器必须是同步函数（v0.7.3 死监听器缺陷的回归钉子）。"""
    coord = _FakeCoordinator(device_type="")
    added: list = []
    await sensor_mod.async_setup_entry(_hass(coord), _entry(), added.extend)
    offenders = [getattr(f, "__name__", repr(f)) for f in coord._listeners
                 if inspect.iscoroutinefunction(f)]
    assert not offenders, f"async def 监听器函数体永不执行: {offenders}"


@pytest.mark.asyncio
async def test_reboot_count_not_double_registered():
    """多次刷新只注册一次（一次性门控）。"""
    coord = _FakeCoordinator(device_type="")
    added: list = []
    await sensor_mod.async_setup_entry(_hass(coord), _entry(), added.extend)

    coord.device_type = "ipcamera"
    coord.system_status = {"rebootCount": "7"}
    coord.data = HikvisionISAPIData(
        device_info={}, system_status=coord.system_status, channels=[],
        capabilities={}, storage={}, network_interfaces=[],
        streaming_channel_detail={},
    )
    coord.fire()
    coord.fire()
    coord.fire()
    assert len(_reboot_keys(added)) == 1, "重复刷新不得重复注册"


@pytest.mark.asyncio
async def test_last_boot_time_always_registered():
    """last_boot_time（12/12 覆盖）应无条件注册，不受 reboot 门控影响。

    它走 deviceUpTime，所有设备都有，所以是静态实体。
    """
    coord = _FakeCoordinator(device_type="")
    added: list = []
    await sensor_mod.async_setup_entry(_hass(coord), _entry(), added.extend)
    keys = {e.entity_description.key for e in added
            if getattr(e, "entity_description", None)}
    assert "last_boot_time" in keys, "last_boot_time 应作为静态实体注册"


@pytest.mark.asyncio
async def test_reboot_value_fn_reads_reported_count():
    """注册的 reboot_count 实体读到设备真实值。"""
    coord = _FakeCoordinator(device_type="")
    added: list = []
    await sensor_mod.async_setup_entry(_hass(coord), _entry(), added.extend)
    coord.device_type = "ipcamera"
    coord.system_status = {"rebootCount": "41"}
    coord.data = HikvisionISAPIData(
        device_info={}, system_status=coord.system_status, channels=[],
        capabilities={}, storage={}, network_interfaces=[],
        streaming_channel_detail={},
    )
    coord.fire()
    ents = _reboot_keys(added)
    assert ents
    assert ents[0].native_value == 41
