"""v0.8 步骤3：last_boot_time（上次启动时间）传感器。

动机：`totalRebootCount` 仅 6/12 台设备返回（176.16/17/18 + 3 台 NVR
无该字段），而 `deviceUpTime` 12/12 台全部可靠。用 `now - uptime` 推算
"上次启动时间"，覆盖面比"重启次数"更广，且能直接回答"设备何时重启过"。

关键设计决策（真机实测驱动）：
  fixtures_v08 中 10.18.176.65 的 currentDeviceTime = 2004-05-05
  （CMOS 电池失效，时钟偏差 8179 天），但 deviceUpTime=317884s 正常。
  → last_boot_time 必须用 **HA 主机时间(utcnow) - uptime**，
    绝不能用 **设备时间 - uptime**，否则 176.65 会显示 2004 年的垃圾值。
  deviceUpTime 是设备自启动以来的单调秒计数，不受时钟错误影响。

测试用固定参考时间驱动纯函数，不依赖 wall-clock，保证可重复。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import sensor as sensor_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    HikvisionISAPIData,
)
from custom_components.hikvision_isapi_performance.const import DOMAIN  # noqa: E402

# 固定参考时刻，避免测试依赖真实时钟。
NOW = datetime(2026, 9, 27, 2, 0, 0, tzinfo=timezone.utc)

# 真机实测的 deviceUpTime（秒），来自 fixtures_v08 的 System/status。
UPTIMES = {
    "10.18.176.10": 172912,
    "10.18.176.12": 317253,
    "10.18.176.13": 317533,
    "10.18.176.16": 852031,
    "10.18.176.17": 852073,
    "10.18.176.18": 317742,
    "10.18.176.51": 11057020,
    "10.18.176.52": 11057233,
    "10.18.176.53": 79219,
    "10.18.176.64": 315698,
    "10.18.176.65": 317884,   # 时钟错误(2004)但 uptime 正常
    "192.168.10.17": 173358,
}


def _data(uptime, device_time=None) -> HikvisionISAPIData:
    status = {}
    if uptime is not None:
        status["uptime"] = str(uptime)
    if device_time is not None:
        status["currentDeviceTime"] = device_time
    return HikvisionISAPIData(
        device_info={"deviceType": "NVR"}, system_status=status,
        channels=[], capabilities={}, storage={}, network_interfaces=[],
        streaming_channel_detail={},
    )


# ── 纯函数：now - uptime ──────────────────────────────────────────


def test_last_boot_uses_host_time_not_device_time():
    """核心：用主机时间算，不受设备时钟错误影响。

    176.65 设备时钟是 2004 年。若实现误用 device_time - uptime，
    会得到 2004-05-02；正确实现用 NOW - uptime，得到 2026-09-23。
    """
    d = _data(317884, device_time="2004-05-05T22:43:44+08:00")
    boot = sensor_mod._last_boot_time(d, now=NOW)
    expected = NOW - timedelta(seconds=317884)
    assert boot == expected
    assert boot.year == 2026, f"被设备错误时钟带偏了: {boot}"
    assert boot != datetime(2004, 5, 2, tzinfo=timezone.utc)


def test_last_boot_all_fleet_devices():
    """12/12 台设备都能算出合理启动时间（uptime 全可靠）。"""
    for host, up in UPTIMES.items():
        boot = sensor_mod._last_boot_time(_data(up), now=NOW)
        assert boot is not None, f"{host} 应能算出启动时间"
        assert boot == NOW - timedelta(seconds=up)
        # 合理性：启动时间必须早于 now、且晚于 2020（uptime 不会大到离谱）
        assert boot < NOW
        assert boot.year >= 2020, f"{host} 启动时间不合理: {boot}"


def test_last_boot_ignores_device_time_field():
    """即便设备时间是 2026 正确的，也必须用主机时间（口径统一）。"""
    d = _data(1000, device_time="2026-09-27T08:00:00+08:00")
    boot = sensor_mod._last_boot_time(d, now=NOW)
    assert boot == NOW - timedelta(seconds=1000)


def test_last_boot_none_when_uptime_missing():
    """无 uptime 字段 → None（不显示垃圾值）。"""
    assert sensor_mod._last_boot_time(_data(None), now=NOW) is None


def test_last_boot_none_when_uptime_zero_or_negative():
    """uptime=0 或负数 → None（设备刚启动或未上报，不显示 1970/未来时间）。"""
    assert sensor_mod._last_boot_time(_data(0), now=NOW) is None
    assert sensor_mod._last_boot_time(_data(-5), now=NOW) is None


def test_last_boot_none_when_uptime_non_numeric():
    """uptime 非数字 → None（容错，不抛异常）。"""
    d = _data(None)
    d.system_status["uptime"] = "abc"
    assert sensor_mod._last_boot_time(d, now=NOW) is None


def test_last_boot_defaults_to_real_clock():
    """不传 now 时用真实主机时钟（生产路径）。"""
    boot = sensor_mod._last_boot_time(_data(60))
    assert boot is not None
    delta = datetime.now(timezone.utc) - boot
    assert 55 <= delta.total_seconds() <= 65, f"应约为60秒前，实得 {delta}"


def test_last_boot_returns_tz_aware_datetime():
    """必须是带时区的 datetime（HA TIMESTAMP 要求 tz-aware）。"""
    boot = sensor_mod._last_boot_time(_data(317884), now=NOW)
    assert isinstance(boot, datetime)
    assert boot.tzinfo is not None


# ── 传感器描述：device_class / category ────────────────────────────


def _find_desc(key):
    return next((d for d in sensor_mod.SENSORS if d.key == key), None)


def test_last_boot_sensor_registered_in_sensors():
    """last_boot_time 必须在 SENSORS 静态表中（所有设备都建）。"""
    desc = _find_desc("last_boot_time")
    assert desc is not None, "SENSORS 缺 last_boot_time 描述"


def test_last_boot_sensor_is_timestamp_diagnostic():
    """device_class=TIMESTAMP，entity_category=DIAGNOSTIC。"""
    desc = _find_desc("last_boot_time")
    assert desc.device_class == "timestamp"
    assert desc.entity_category == "diagnostic"


def test_last_boot_value_fn_uses_host_time():
    """描述里的 value_fn 走 _last_boot_time（主机时间口径）。"""
    desc = _find_desc("last_boot_time")
    d = _data(317884, device_time="2004-05-05T22:43:44+08:00")
    val = desc.value_fn(d)
    assert val is not None
    assert val.year == 2026, f"value_fn 被设备时钟带偏: {val}"


# ── 步骤7 关联：reboot_count 按能力创建 ────────────────────────────
#
# reboot_count 仍留在 SENSORS 表（保持 unique_id 稳定），但注册时必须
# 门控：设备没有 totalRebootCount 字段就不建实体。门控逻辑在
# async_setup_entry，测试见 test_v08_capability_gating.py。这里只锁
# value_fn 对"设备无该字段"返回 None（显示 unknown 而非 0）。


def test_reboot_count_none_when_field_absent():
    """设备无 totalRebootCount → value_fn 返回 None（不谎报 0 次重启）。"""
    desc = _find_desc("reboot_count")
    assert desc is not None
    # 176.16 实测无该字段
    d = _data(852031)  # system_status 无 rebootCount
    assert desc.value_fn(d) is None


def test_reboot_count_value_when_present():
    """设备有 totalRebootCount=41 → 返回 41。"""
    desc = _find_desc("reboot_count")
    d = _data(172912)
    d.system_status["rebootCount"] = "41"
    assert desc.value_fn(d) == 41
