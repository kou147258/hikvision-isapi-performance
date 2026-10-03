"""v0.8 步骤12 + 决策3：HA 实体规范化与存储误导值默认禁用。

决策3（用户选定"保留但默认禁用"）：
  实测 12 台有盘设备 freeSpace 全部返回 0（workMode=quota 循环覆盖模式）。
  于是 storage_free_gb 长期显示 0 GB、storage_usage_percent 长期显示 100%
  —— 都是误导值。用户决定：保留这两个传感器但默认禁用（entity_registry_
  enabled_default=False），需要时在实体注册表打开。
  存储总量 storage_total_gb 是真实值，保持启用。

步骤12（HA 原生规范化）：
  * 诊断类只读遥测 → entity_category=DIAGNOSTIC，不进自动仪表盘
  * 可出统计图表的量 → state_class=MEASUREMENT
  * device_class 正确（DATA_SIZE / DURATION / TIMESTAMP / ENUMERATION）

本文件用 SENSORS 静态表 + 动态 builder 的产物做断言，全部离线。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import sensor as sensor_mod  # noqa: E402


def _desc(key):
    d = next((x for x in sensor_mod.SENSORS if x.key == key), None)
    assert d is not None, f"SENSORS 缺 {key}"
    return d


# ── 决策3：freeSpace=0 的误导值默认禁用 ────────────────────────────


def test_storage_free_disabled_by_default():
    """存储剩余长期显示 0 GB（设备 quota 模式不报真实剩余）→ 默认禁用。"""
    assert _desc("storage_free_gb").entity_registry_enabled_default is False


def test_storage_usage_disabled_by_default():
    """存储使用率长期显示 100%（同上）→ 默认禁用。"""
    assert _desc("storage_usage_percent").entity_registry_enabled_default is False


def test_storage_total_stays_enabled():
    """存储总量是真实值 → 保持默认启用，不被连带禁用。"""
    assert _desc("storage_total_gb").entity_registry_enabled_default is True


def test_storage_total_is_measurement_data_size():
    """总量：DATA_SIZE + MEASUREMENT（可出图表）。"""
    d = _desc("storage_total_gb")
    assert d.device_class == "data_size"
    assert d.state_class == "measurement"


# ── 步骤12：诊断类实体标注 ─────────────────────────────────────────


def test_diagnostic_sensors_marked():
    """只读系统遥测标 DIAGNOSTIC，避免淹没自动仪表盘。"""
    for key in ("model", "serial_number", "firmware_version", "device_type",
                "device_mac", "last_boot_time"):
        d = _desc(key)
        assert d.entity_category == "diagnostic", f"{key} 应标 DIAGNOSTIC"


def test_last_boot_time_is_timestamp_diagnostic():
    """上次启动时间：TIMESTAMP + DIAGNOSTIC。"""
    d = _desc("last_boot_time")
    assert d.device_class == "timestamp"
    assert d.entity_category == "diagnostic"


def test_measurement_sensors_have_state_class():
    """可统计的量必须有 state_class，否则 HA 不画长期统计图。"""
    for key in ("cpu_usage", "memory_usage_percent", "memory_available_mb"):
        d = _desc(key)
        assert d.state_class in ("measurement", "total_increasing"), (
            f"{key} 缺 state_class")


def test_uptime_is_duration_measurement():
    """运行时长：DURATION + total_increasing。"""
    d = _desc("uptime_hours")
    assert d.device_class == "duration"
    assert d.state_class == "total_increasing"


# ── 步骤12：动态生成的逐盘/每通道实体也遵守规范 ────────────────────


def test_per_hdd_status_is_enumeration_diagnostic():
    """逐盘 status 是 ENUMERATION + DIAGNOSTIC。"""
    from tests.test_v08_hdd_entities import _storage, _data, _Coord, _entry
    storage = _storage("10.18.176.64")
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    st = next(e for e in ents if e.entity_description.key == "hdd_1_status")
    assert st.entity_description.device_class == "enum"
    assert st.entity_description.entity_category == "diagnostic"


def test_hdd_error_count_is_measurement():
    """HDD 异常数：MEASUREMENT（可统计）。"""
    from tests.test_v08_hdd_entities import _storage, _data, _Coord, _entry
    storage = _storage("10.18.176.64")
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    ec = next(e for e in ents if e.entity_description.key == "hdd_error_count")
    assert ec.entity_description.state_class == "measurement"
    assert ec.entity_description.entity_category == "diagnostic"


def test_per_channel_bitrate_is_measurement():
    """每通道码率：DATA_RATE + MEASUREMENT。"""
    from custom_components.hikvision_isapi_performance.coordinator import (
        HikvisionISAPIData,
    )
    from custom_components.hikvision_isapi_performance.const import DOMAIN
    data = HikvisionISAPIData(
        device_info={}, system_status={}, channels=[], capabilities={},
        storage={}, network_interfaces=[], streaming_channel_detail={},
    )
    coord = type("C", (), {
        "data": data, "storage": {}, "recording_status": {},
        "duplicate_channels": set(), "_host": "h",
    })()
    entry = type("E", (), {"entry_id": "conv", "data": {}, "options": {}})()
    ents = sensor_mod._build_per_channel_entities(
        coord, entry, {"id": "1", "name": "摄像机12"})
    br = next(e for e in ents
              if e.entity_description.key == "channel_1_video_bitrate")
    assert br.entity_description.state_class == "measurement"
    assert br.entity_description.device_class == "data_rate"


# ── 步骤12 补充：每通道二值传感器不得设 translation_key ───────────
#
# 真实 HA 里 translation_key 优先于 name（项目 v0.7.5 在 sensor.py 已
# 记录这一结论）。每通道实体若设了 translation_key，所有通道会渲染成
# 同一固定标签，摄像机名丢失 —— 连带本轮加的「（推导）」标注也会消失，
# 而那正是承诺给用户的关键诚实信息。conftest 桩不模拟该优先级，故测试
# 全绿也发现不了；这里直接锁 _attr_translation_key 为 None。
#
# 设备级实体（device_online / dev_time_abnormal 等）设 translation_key
# 是对的，问题只在"每通道"实体。


def _bin_coord_entry():
    from custom_components.hikvision_isapi_performance.coordinator import (
        HikvisionISAPIData,
    )
    data = HikvisionISAPIData(
        device_info={}, system_status={}, channels=[], capabilities={},
        storage={}, network_interfaces=[], streaming_channel_detail={},
    )
    coord = type("C", (), {
        "data": data, "channels": [], "storage": {}, "recording_status": {},
        "event_state": {}, "event_meta": {}, "duplicate_channels": set(),
        "_host": "h",
    })()
    entry = type("E", (), {"entry_id": "conv", "data": {}, "options": {}})()
    return coord, entry


def test_per_channel_binary_sensors_have_no_translation_key():
    """在线/录像中/运动检测三个每通道实体都不得设 translation_key。"""
    from custom_components.hikvision_isapi_performance import binary_sensor as bs
    coord, entry = _bin_coord_entry()
    for cls in (
        bs.HikvisionISAPIChannelOnlineBinarySensor,
        bs.HikvisionISAPIChannelRecordingBinarySensor,
        bs.HikvisionISAPIChannelMotionBinarySensor,
    ):
        ent = cls(coord, entry, "1", "摄像机12")
        assert getattr(ent, "_attr_translation_key", None) is None, (
            f"{cls.__name__} 设了 translation_key，真实 HA 会用它覆盖"
            f"含摄像机名的 _attr_name，导致所有通道显示同一标签"
        )


def test_per_channel_binary_names_carry_camera_name():
    """三个每通道实体名都带摄像机名（去重后仍保留）。"""
    from custom_components.hikvision_isapi_performance import binary_sensor as bs
    coord, entry = _bin_coord_entry()
    online = bs.HikvisionISAPIChannelOnlineBinarySensor(coord, entry, "3", "摄像机09")
    rec = bs.HikvisionISAPIChannelRecordingBinarySensor(coord, entry, "3", "摄像机09")
    motion = bs.HikvisionISAPIChannelMotionBinarySensor(coord, entry, "3", "摄像机09")
    assert "摄像机09" in online._attr_name
    assert "摄像机09" in rec._attr_name
    assert "摄像机09" in motion._attr_name


def test_recording_name_marks_derived():
    """录像实体名保留「推导」标注（不能被 translation_key 遮蔽）。"""
    from custom_components.hikvision_isapi_performance import binary_sensor as bs
    coord, entry = _bin_coord_entry()
    rec = bs.HikvisionISAPIChannelRecordingBinarySensor(coord, entry, "1", "摄像机12")
    assert "推导" in rec._attr_name
    assert getattr(rec, "_attr_translation_key", None) is None


# ── 同类缺陷：PTZ 方向按钮共用一个 translation_key ────────────────
#
# 4 个方向按钮（上/下/左/右）各有不同的 _attr_name，却共用
# translation_key="ptz_direction"。真实 HA 里 translation_key 优先于
# name，一旦该翻译键被补上，4 个按钮会显示成同一标签。当前只因翻译键
# 在 4 个语言文件里都缺失、HA 回退到 _attr_name 才侥幸正常。这与每通道
# 二值传感器是同一类 bug，一并修掉：方向按钮不设 translation_key。


def test_ptz_direction_buttons_have_no_translation_key():
    """4 个方向按钮不得共用 translation_key（会遮蔽各自方向名）。"""
    from custom_components.hikvision_isapi_performance import button as btn
    coord, entry = _bin_coord_entry()
    for d in btn._PTZ_DIRECTIONS:
        b = btn.HikvisionISAPIPTZButton(
            coord, entry, key=d["key"], command=d["command"], name=d["name"])
        assert getattr(b, "_attr_translation_key", None) is None, (
            f"PTZ 按钮 {d['key']} 设了共用 translation_key，会遮蔽方向名 {d['name']}"
        )


def test_ptz_direction_buttons_carry_distinct_names():
    """4 个方向按钮各带独立方向名。"""
    from custom_components.hikvision_isapi_performance import button as btn
    coord, entry = _bin_coord_entry()
    names = []
    for d in btn._PTZ_DIRECTIONS:
        b = btn.HikvisionISAPIPTZButton(
            coord, entry, key=d["key"], command=d["command"], name=d["name"])
        names.append(b._attr_name)
    assert len(set(names)) == 4, f"4 个方向名应互不相同，实得 {names}"


def test_reboot_button_keeps_translation_key():
    """reboot 是设备级单例，翻译键正确齐全 → 保留（与方向按钮区别对待）。"""
    from custom_components.hikvision_isapi_performance import button as btn
    coord, entry = _bin_coord_entry()
    b = btn.HikvisionISAPIRebootButton(coord, entry)
    assert b._attr_translation_key == "reboot"


