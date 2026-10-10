"""v0.9 批次1：把 /System/status 里被丢弃的字段变成 HA 实体。

实体清单（全部按真机覆盖率门控，设备不上报就不建实体）：

  球机寿命 dome_info（真机 6/12：176.10/12/13/51/52/53）
    dome_run_time              云台累计运行时长（小时）
    dome_pan_rounds            水平累计转动圈数
    dome_tilt_rounds           垂直累计转动圈数
    dome_runtime_below_neg20   低于 -20°C 运行时长（小时）
    dome_runtime_normal        -20~40°C 运行时长（小时）
    dome_runtime_above_pos40   高于 40°C 运行时长（小时）
    dome_heater_active         加热器运行中（binary）
    dome_fan_active            风扇运行中（binary）

  镜头计数 camera_usage（真机同 6 台）
    camera_run_time            镜头累计运行时长（小时）
    camera_zoom_steps          变焦累计步数
    camera_focus_steps         对焦累计步数
    camera_iris_steps          光圈累计步数
    camera_icr_steps           ICR 累计步数
    camera_focus_reverse_times 对焦往返次数（高值 = 自动对焦在来回搜索）

  其它
    memory_type                内存类型（真机 12/12）
    cpu_description            CPU 描述（真机 11/12，R17.17 无 CPUList）
                               注：标签用「描述」而非「型号」—— 176.65 该字段
                               返回纯数字 2786.91（两次抓包一致，固件稳定行为），
                               称其为型号会误导；也不断言单位是 MHz（无法验证）
    battery_allowance          电池余量（真机 1/12，仅 176.10）
    sd_card_rewrite_times      SD 卡重写次数（真机 1/12，仅 176.10）

设计约束：
  * 全部 entity_category=DIAGNOSTIC（只读诊断遥测，不进默认仪表盘）
  * 累计量用 TOTAL_INCREASING + 小时（与既有 uptime_hours 一致）
  * 这些是 per-device 单实例实体，故【使用 translation_key】。
    注意与 v0.8 教训的区别：v0.8 的缺陷是 per-channel 多实例共享一个 key
    导致所有通道渲染成同一标签；这里每台设备只有一个实例，key 安全。
  * 迟到数据：首次刷新是后台任务，setup 时 system_status 可能还是空 dict，
    故必须走 late-arrival listener（与 per-hdd / reboot_count 同模式）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import sensor as sensor_mod  # noqa: E402
from custom_components.hikvision_isapi_performance import binary_sensor as bs_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    _parse_system_status,
)
from custom_components.hikvision_isapi_performance.isapi_client import (  # noqa: E402
    _strip_xmlns,
)
from custom_components.hikvision_isapi_performance.const import DOMAIN  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures_v09"

IP2KEY = {
    "176.10": "10_18_176_10", "176.12": "10_18_176_12", "176.13": "10_18_176_13",
    "176.16": "10_18_176_16", "176.17": "10_18_176_17", "176.18": "10_18_176_18",
    "176.51": "10_18_176_51", "176.52": "10_18_176_52", "176.53": "10_18_176_53",
    "176.64": "10_18_176_64", "176.65": "10_18_176_65", "R17.17": "192_168_10_17",
}
DOME_HOSTS = ["176.10", "176.12", "176.13", "176.51", "176.52", "176.53"]
NO_DOME_HOSTS = ["176.16", "176.17", "176.18", "176.64", "176.65", "R17.17"]


def _status(host: str) -> dict[str, Any]:
    """用真机 fixture 跑生产解析器，得到 system_status。"""
    f = FIXTURES / f"{IP2KEY[host]}__G_status__System_status.xml"
    assert f.exists(), f"missing fixture {f.name}"
    root = ET.fromstring(_strip_xmlns(f.read_text(encoding="utf-8", errors="replace")))
    return _parse_system_status(root)


class _Coord:
    """最小 coordinator 替身，仅提供实体构建所需属性。"""

    def __init__(self, system_status: dict[str, Any]):
        self.system_status = system_status
        self.data = None
        self.channels = []
        self.network_interfaces = []
        self.device_type = "ipcamera"
        self.device_info = {"deviceType": "IPCamera", "firmwareVersion": "V5.10.0"}
        self.capabilities = {}
        self.storage = {}
        self.streaming_channel_detail = {}
        self.unique_id = f"{DOMAIN}_status"
        self._host = "10.18.176.10"
        self._port = 80
        self._username = "admin"
        self._password = "pw"
        self._verify_ssl = False
        self._use_https = False
        self._listeners = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: None


def _entry():
    e = type("E", (), {})()
    e.entry_id = "status_test"
    e.data = {"host": "10.18.176.10"}
    e.options = {}
    return e


def _build(host: str):
    """调用被测 builder，返回实体列表。"""
    return sensor_mod._build_status_extras_entities(_Coord(_status(host)), _entry())


def _by_key(entities, key: str):
    for e in entities:
        if e.entity_description.key == key:
            return e
    return None


# ══════════════════════════════════════════════════════════════════
# 1. 门控：只有真机上报的设备才建实体
# ══════════════════════════════════════════════════════════════════


def test_dome_entities_built_only_on_dome_devices():
    """球机实体只在真机有 DomeInfoList 的 6 台上创建。

    这是用户诉求「有部分还是显示未知」的直接对策：不给没有数据源的
    设备建实体，就不会出现永久 unknown。
    """
    for host in DOME_HOSTS:
        keys = {e.entity_description.key for e in _build(host)}
        assert "dome_run_time" in keys, f"{host} 是球机，应有球机实体"
        assert "dome_pan_rounds" in keys, host
        assert "dome_tilt_rounds" in keys, host


def test_dome_entities_absent_on_non_dome_devices():
    """定焦 IPC 与录像机不得产生任何球机/镜头实体。"""
    for host in NO_DOME_HOSTS:
        keys = {e.entity_description.key for e in _build(host)}
        dome_keys = {k for k in keys if k.startswith(("dome_", "camera_"))}
        assert not dome_keys, f"{host} 不该有球机/镜头实体，实际 {dome_keys}"


def test_memory_type_built_on_all_devices():
    """memory_type 真机 12/12 全设备可用，必须每台都建。"""
    for host in IP2KEY:
        assert _by_key(_build(host), "memory_type") is not None, host


def test_cpu_description_built_except_one_nvr():
    """cpu_description 真机 11/12：R17.17 无 CPUList，不得建实体。"""
    for host in IP2KEY:
        ent = _by_key(_build(host), "cpu_description")
        if host == "R17.17":
            assert ent is None, "R17.17 无 cpuDescription，不该建 cpu_description"
        else:
            assert ent is not None, f"{host} 应有 cpu_description"


def test_battery_and_sd_built_only_on_one_device():
    """battery_allowance / sd_card_rewrite_times 真机仅 176.10 有。"""
    for host in IP2KEY:
        ents = {e.entity_description.key for e in _build(host)}
        if host == "176.10":
            assert "battery_allowance" in ents, host
            assert "sd_card_rewrite_times" in ents, host
        else:
            assert "battery_allowance" not in ents, f"{host} 不该有电池实体"
            assert "sd_card_rewrite_times" not in ents, f"{host} 不该有SD实体"


def test_empty_status_builds_nothing():
    """首次刷新前 system_status 为空 dict → 不建任何实体（由 listener 补注册）。"""
    ents = sensor_mod._build_status_extras_entities(_Coord({}), _entry())
    assert ents == []


# ══════════════════════════════════════════════════════════════════
# 2. 数值正确性（必须等于真机值，不得用猜测值）
# ══════════════════════════════════════════════════════════════════


class _DataProxy:
    """实体 native_value 从 coordinator.data 读，测试直接喂 system_status。"""

    def __init__(self, system_status):
        self.system_status = system_status
        self.device_info = {}
        self.channels = []
        self.storage = {}
        self.network_interfaces = []
        self.recording_status = {}
        self.time_info = {}


def _value(host: str, key: str):
    """取指定设备指定实体的 native_value。"""
    coord = _Coord(_status(host))
    ent = None
    for e in sensor_mod._build_status_extras_entities(coord, _entry()):
        if e.entity_description.key == key:
            ent = e
            break
    assert ent is not None, f"{host} 未建实体 {key}"
    coord.data = _DataProxy(coord.system_status)
    return ent.native_value


def test_dome_run_time_is_hours_not_seconds():
    """176.10 真机 domeRunTotalTime=608305 秒 → 169.0 小时（保留1位小数）。

    与既有 uptime_hours 同口径，人类可读。若误把秒当小时会得 608305。
    """
    assert _value("176.10", "dome_run_time") == 169.0


def test_dome_rounds_are_exact_real_values():
    """176.10 真机 panTotalRounds=62, tiltTotalRounds=68。"""
    assert _value("176.10", "dome_pan_rounds") == 62
    assert _value("176.10", "dome_tilt_rounds") == 68


def test_dome_temperature_buckets_exact_real_values():
    """176.10 三温度区间真机秒数 0 / 248171 / 360134 → 小时。"""
    assert _value("176.10", "dome_runtime_below_neg20") == 0.0
    assert _value("176.10", "dome_runtime_normal") == 68.9
    assert _value("176.10", "dome_runtime_above_pos40") == 100.0


def test_dome_zero_counters_render_zero_not_none():
    """176.13 球机字段真机全为 0 → 必须显示 0，不得显示 unknown。

    0 是有效读数（球机从未转动过），与字段缺失是两回事。
    """
    assert _value("176.13", "dome_run_time") == 0.0
    assert _value("176.13", "dome_pan_rounds") == 0
    assert _value("176.13", "dome_tilt_rounds") == 0


def test_camera_usage_exact_real_values():
    """176.10 镜头计数真机值。"""
    assert _value("176.10", "camera_zoom_steps") == 890
    assert _value("176.10", "camera_focus_steps") == 121
    assert _value("176.10", "camera_iris_steps") == 1037
    assert _value("176.10", "camera_icr_steps") == 194
    assert _value("176.10", "camera_focus_reverse_times") == 9417


def test_camera_run_time_hours():
    """176.10 cameraRunTotalTime=608305 秒 → 169.0 小时。"""
    assert _value("176.10", "camera_run_time") == 169.0


def test_memory_type_value():
    """真机 memoryDescription="DDR Memory"（12/12 一致）。"""
    assert _value("176.10", "memory_type") == "DDR Memory"
    assert _value("R17.17", "memory_type") == "DDR Memory"


def test_cpu_description_value():
    """176.10 真机 cpuDescription 完整字符串。"""
    assert _value("176.10", "cpu_description") == (
        "ARM926EJ-Sid(wb) [41069265] revision 5 (ARMv5TEJ)"
    )


def test_cpu_description_label_is_not_model_specific():
    """★标签必须是「CPU 描述」，不得叫「CPU 型号」。

    真机核查（fixture 与今日新抓包一致，非瞬时抖动）：
      11 台返回型号名，如 "ARM926EJ-Sid(wb)…"、"ARMv7 Processor rev 1 (v7l)"
      176.65 (DS-7708N-I4 V4.1.18 DVR) 返回 "2786.91" —— 纯数字，不是型号名

    把 2786.91 标成「CPU 型号」会误导。同时【不得】把它断言成 MHz：
    单一数值无法验证单位，猜错单位比不显示更糟（违反项目「不编造、
    不猜单位」约束）。故采用 XML 字段名的字面含义「CPU 描述」，
    对两种取值都不失实。
    """
    ent = _by_key(_build("176.10"), "cpu_description")
    assert ent is not None
    name = ent.entity_description.name
    assert "型号" not in name, f"标签不得称型号，实得 {name!r}"
    assert name == "CPU 描述"


def test_cpu_description_renders_numeric_value_as_is():
    """176.65 的数字值必须原样呈现，不做单位换算、不四舍五入、不猜测。"""
    assert _value("176.65", "cpu_description") == "2786.91"


def test_battery_and_sd_values():
    """176.10 真机 batteryAllowance=0（有效值）, videoRewritingTimes=3706。"""
    assert _value("176.10", "battery_allowance") == 0
    assert _value("176.10", "sd_card_rewrite_times") == 3706


# ══════════════════════════════════════════════════════════════════
# 3. HA 约定（entity_category / state_class / unique_id）
# ══════════════════════════════════════════════════════════════════


def test_all_new_sensors_are_diagnostic():
    """只读诊断遥测必须 entity_category=DIAGNOSTIC，不进默认仪表盘。"""
    from homeassistant.helpers.entity import EntityCategory

    ents = _build("176.10")
    assert ents, "176.10 应有实体"
    for e in ents:
        assert e.entity_description.entity_category == EntityCategory.DIAGNOSTIC, (
            f"{e.entity_description.key} 缺 DIAGNOSTIC"
        )


def test_cumulative_sensors_are_total_increasing():
    """累计量（运行时长/圈数/步数）必须 TOTAL_INCREASING，HA 才能画累计曲线。"""
    from homeassistant.components.sensor import SensorStateClass

    total_increasing = {
        "dome_run_time", "dome_pan_rounds", "dome_tilt_rounds",
        "dome_runtime_below_neg20", "dome_runtime_normal", "dome_runtime_above_pos40",
        "camera_run_time", "camera_zoom_steps", "camera_focus_steps",
        "camera_iris_steps", "camera_icr_steps", "camera_focus_reverse_times",
        "sd_card_rewrite_times",
    }
    for e in _build("176.10"):
        key = e.entity_description.key
        if key in total_increasing:
            assert e.entity_description.state_class == SensorStateClass.TOTAL_INCREASING, key


def test_duration_sensors_use_hours_unit():
    """时长类实体单位必须是小时（与 uptime_hours 一致），否则数值不可读。"""
    from homeassistant.const import UnitOfTime
    from homeassistant.components.sensor import SensorDeviceClass

    duration_keys = {
        "dome_run_time", "dome_runtime_below_neg20", "dome_runtime_normal",
        "dome_runtime_above_pos40", "camera_run_time",
    }
    for e in _build("176.10"):
        key = e.entity_description.key
        if key in duration_keys:
            d = e.entity_description
            assert d.native_unit_of_measurement == UnitOfTime.HOURS, key
            assert d.device_class == SensorDeviceClass.DURATION, key


def test_unique_ids_are_distinct():
    """unique_id 必须互不相同，否则 HA 实体注册表会冲突。

    访问 ``_attr_unique_id`` 而非 ``unique_id``：conftest 的实体 stub 没有
    真实 HA 基类那个把 ``_attr_unique_id`` 暴露成 property 的实现，
    既有 v08 测试也一律用 ``_attr_unique_id``。
    """
    ids = [e._attr_unique_id for e in _build("176.10")]
    assert len(ids) == len(set(ids)), f"unique_id 重复: {ids}"
    assert all(i.startswith("status_test_") for i in ids), ids


def test_translation_keys_are_set_for_single_instance_entities():
    """这些是 per-device 单实例实体，必须设 translation_key 以支持多语言。

    与 v0.8 的区别：v0.8 的缺陷是 per-channel 多实例共享一个 key（所有通道
    渲染成同一标签）。这里每台设备只有一个实例，translation_key 是正确做法。
    """
    for e in _build("176.10"):
        assert e.entity_description.translation_key, (
            f"{e.entity_description.key} 缺 translation_key"
        )


# ══════════════════════════════════════════════════════════════════
# 4. binary_sensor：加热器 / 风扇
#
# 注意实现约定：binary_sensor.py 的既有实体（HddProblem / Event）都是
# 【手写类】模式 —— 用 _sensor_key 属性 + _attr_unique_id/_attr_name，
# 而非 sensor.py 的 entity_description 模式（conftest 也没有 stub
# BinarySensorEntityDescription）。所以这里断言 _sensor_key，
# 保持与既有代码一致，不为两个实体引入第二种范式。
# ══════════════════════════════════════════════════════════════════


def _build_binary(host: str | None = None, *, coord=None):
    """按设备名或已构造的 coordinator 生成球机二值实体。

    两种调用方式都需要：门控测试用 host（每台设备各自建），
    状态测试用 coord（需先把 data 塞进去才能读 is_on）。
    """
    if coord is None:
        assert host is not None
        coord = _Coord(_status(host))
    return bs_mod._build_dome_state_entities(coord, _entry())


def _binary_by_key(entities, key: str):
    for e in entities:
        if getattr(e, "_sensor_key", None) == key:
            return e
    return None


def test_dome_binary_sensors_only_on_dome_devices():
    """加热器/风扇二值传感器只在球机上创建。"""
    for host in DOME_HOSTS:
        keys = {e._sensor_key for e in _build_binary(host)}
        assert "dome_heater_active" in keys, host
        assert "dome_fan_active" in keys, host
    for host in NO_DOME_HOSTS:
        assert _build_binary(host) == [], f"{host} 不该有球机二值传感器"


def test_dome_binary_states_are_exact_real_values():
    """176.10 真机 heatState=0, fanState=1 → 加热器 off, 风扇 on。"""
    coord = _Coord(_status("176.10"))
    coord.data = _DataProxy(coord.system_status)
    ents = _build_binary(coord=coord)
    assert _binary_by_key(ents, "dome_heater_active").is_on is False
    assert _binary_by_key(ents, "dome_fan_active").is_on is True


def test_dome_binary_all_off_on_idle_device():
    """176.13 真机 heatState=0, fanState=0 → 两者都 off（0 是有效值）。"""
    coord = _Coord(_status("176.13"))
    coord.data = _DataProxy(coord.system_status)
    ents = _build_binary(coord=coord)
    assert _binary_by_key(ents, "dome_heater_active").is_on is False
    assert _binary_by_key(ents, "dome_fan_active").is_on is False


def test_dome_binary_are_diagnostic():
    """球机机械状态属诊断信息，不得进默认仪表盘。"""
    from homeassistant.helpers.entity import EntityCategory

    for e in _build_binary("176.10"):
        assert e._attr_entity_category == EntityCategory.DIAGNOSTIC, e._sensor_key


def test_dome_binary_unique_ids_distinct():
    """unique_id 必须互不相同且带 entry_id 前缀。"""
    ids = [e._attr_unique_id for e in _build_binary("176.10")]
    assert len(ids) == len(set(ids)), ids
    assert all(i.startswith("status_test_") for i in ids), ids


def test_dome_binary_none_when_data_not_yet_available():
    """coordinator.data 为 None（首次刷新前）→ is_on 返回 None（unknown），
    不得抛异常，也不得谎报 off。"""
    coord = _Coord(_status("176.10"))
    coord.data = None
    for e in _build_binary(coord=coord):
        assert e.is_on is None, e._sensor_key


# ══════════════════════════════════════════════════════════════════
# 5. 迟到数据：listener 必须在刷新后补注册，且不重复
# ══════════════════════════════════════════════════════════════════


def test_listener_registers_entities_after_refresh():
    """setup 时 system_status 为空 → listener 在数据到达后补注册。

    这是 v0.7.3 修过的死监听器陷阱的正面验证：listener 必须是普通
    sync 函数（async def 会导致函数体永不执行）。
    """
    coord = _Coord({})
    added: list[Any] = []
    listener = sensor_mod._make_status_extras_listener(
        coord, _entry(), lambda ents: added.extend(ents)
    )
    # 必须是普通函数，不是协程函数
    import inspect
    assert not inspect.iscoroutinefunction(listener), (
        "listener 若是 async def，HA 同步调用它时函数体永不执行"
    )

    listener()
    assert added == [], "无数据时不该注册"

    coord.system_status = _status("176.10")
    listener()
    assert added, "数据到达后应注册实体"
    keys = {e.entity_description.key for e in added}
    assert "dome_run_time" in keys and "memory_type" in keys


def test_listener_is_one_shot():
    """listener 只能注册一次，否则每次刷新都会重复添加实体。"""
    coord = _Coord(_status("176.10"))
    added: list[Any] = []
    listener = sensor_mod._make_status_extras_listener(
        coord, _entry(), lambda ents: added.extend(ents)
    )
    listener()
    first = len(added)
    assert first > 0
    listener()
    listener()
    assert len(added) == first, "重复注册会导致 HA 实体冲突"


def test_binary_listener_is_one_shot_and_sync():
    """球机二值传感器的 listener 同样必须一次性且为普通函数。"""
    import inspect

    coord = _Coord({})
    added: list[Any] = []
    listener = bs_mod._make_dome_state_listener(
        coord, _entry(), lambda ents: added.extend(ents)
    )
    assert not inspect.iscoroutinefunction(listener)
    listener()
    assert added == []
    coord.system_status = _status("176.10")
    listener()
    n = len(added)
    assert n == 2, f"应注册加热器+风扇两个实体，实际 {n}"
    listener()
    assert len(added) == n, "重复注册"
