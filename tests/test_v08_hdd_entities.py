"""v0.8 步骤2：逐盘健康实体。

步骤1 已让 `_parse_storage` 返回 `hdds` 逐盘明细。本步把它变成 HA 实体：

  * 每块物理存在的盘 → 一个 status 枚举传感器（ENUMERATION，DIAGNOSTIC）
  * 每块物理存在的盘 → 一个 problem 二值传感器（PROBLEM）
  * 一个聚合 HDD 异常数传感器（MEASUREMENT）

设计约束（全部来自真机实测）：
  * 空槽 notexist 已在 parser 层跳过，不出现在 hdds → 不建实体
  * 盘数因设备而异（176.64=4 块, 176.65=1 块, 9 台 IPC=0 块）→ 必须动态生成
  * 盘实体是迟到数据（storage 端点在首次刷新后才可用）→ 走迟到监听器
  * 无盘设备（IPC）不得产生任何盘实体

测试用真机 fixture + 构造的 error 盘验证，不访问网络。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import sensor as sensor_mod  # noqa: E402
from custom_components.hikvision_isapi_performance import binary_sensor as bs_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    HikvisionISAPIData,
    _parse_storage,
)
from custom_components.hikvision_isapi_performance.const import DOMAIN  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures_v08"


def _storage(host: str) -> dict[str, Any]:
    f = FIXTURES / f"{host.replace('.', '_')}__C_hdd__ISAPI_ContentMgmt_storage.xml"
    assert f.exists(), f"missing fixture {f.name}"
    root = ET.fromstring(f.read_text(encoding="utf-8", errors="replace"))
    return _parse_storage(root)


def _data(storage: dict[str, Any]) -> HikvisionISAPIData:
    return HikvisionISAPIData(
        device_info={"deviceType": "NVR", "firmwareVersion": "V4.61.030"},
        system_status={}, channels=[], capabilities={},
        storage=storage, network_interfaces=[],
        streaming_channel_detail={},
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
        self.unique_id = f"{DOMAIN}_hdd"
        self._host = "10.18.176.64"
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
    e.entry_id = "hdd_test"
    e.data = {"host": "10.18.176.64"}
    e.options = {}
    return e


# ── 传感器：逐盘 status + HDD 异常数 ────────────────────────────────


def test_build_per_hdd_sensors_for_4_disk_nvr():
    """176.64（hdd3 空槽已跳过）→ 4 块盘 → 4 个 status 传感器 + 1 个异常数。"""
    storage = _storage("10.18.176.64")
    assert len(storage["hdds"]) == 4  # parser 已排除空槽
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    keys = [e.entity_description.key for e in ents]
    # 每块盘一个 status 传感器
    for hid in ("1", "2", "4", "5"):
        assert f"hdd_{hid}_status" in keys, f"缺 hdd_{hid}_status"
    # 一个聚合异常数传感器
    assert "hdd_error_count" in keys
    # 空槽不得有实体
    assert "hdd_3_status" not in keys


def test_per_hdd_status_values():
    """status 传感器读到每块盘的真实状态值。"""
    storage = _storage("10.18.176.64")
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    by_key = {e.entity_description.key: e for e in ents}
    assert by_key["hdd_1_status"].native_value == "ok"
    assert by_key["hdd_5_status"].native_value == "ok"


def test_per_hdd_status_is_enumeration_diagnostic():
    """逐盘 status 是 ENUMERATION + DIAGNOSTIC，不进自动仪表盘。"""
    storage = _storage("10.18.176.64")
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    st = next(e for e in ents if e.entity_description.key == "hdd_1_status")
    assert st.entity_description.device_class == "enum"
    assert st.entity_description.entity_category == "diagnostic"
    # ENUMERATION 必须给 options，否则 HA 报警告
    assert st.entity_description.options


def test_hdd_error_count_is_measurement():
    """HDD 异常数是 MEASUREMENT，可出统计图表。"""
    storage = _storage("10.18.176.64")
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    ec = next(e for e in ents if e.entity_description.key == "hdd_error_count")
    assert ec.entity_description.state_class == "measurement"
    assert ec.native_value == 0


def test_hdd_error_count_nonzero_on_faulty_disk():
    """有 error 盘时异常数 > 0。"""
    xml = """<storage version="2.0"><hddList>
      <hdd><id>1</id><hddName>hdd1</hddName><status>ok</status>
        <capacity>1000</capacity><freeSpace>0</freeSpace></hdd>
      <hdd><id>2</id><hddName>hdd2</hddName><status>error</status>
        <capacity>1000</capacity><freeSpace>0</freeSpace></hdd>
      </hddList></storage>"""
    storage = _parse_storage(ET.fromstring(xml))
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    ec = next(e for e in ents if e.entity_description.key == "hdd_error_count")
    assert ec.native_value == 1
    # error 盘的 status 传感器读到原始值
    st = next(e for e in ents if e.entity_description.key == "hdd_2_status")
    assert st.native_value == "error"


def test_single_disk_nvr():
    """176.65 单盘 → 1 个 status 传感器 + 1 个异常数。"""
    storage = _storage("10.18.176.65")
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    keys = [e.entity_description.key for e in ents]
    assert "hdd_1_status" in keys
    assert "hdd_error_count" in keys
    assert "hdd_2_status" not in keys


def test_ipc_without_disks_no_hdd_entities():
    """9 台 IPC 无盘 → 不产生任何盘实体（连异常数也不建，避免永久 0）。"""
    storage = _storage("10.18.176.18")
    assert storage["hdds"] == []
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    assert ents == []


def test_per_hdd_unique_id_stable():
    """unique_id 含盘 id，稳定且互不冲突。"""
    storage = _storage("10.18.176.64")
    ents = sensor_mod._build_per_hdd_entities(_Coord(_data(storage)), _entry())
    uids = [e._attr_unique_id for e in ents]
    assert len(uids) == len(set(uids)), "unique_id 有冲突"
    assert any("hdd_1_status" in u for u in uids)


# ── 二值传感器：逐盘 problem ────────────────────────────────────────


def test_build_per_hdd_problem_binary_sensors():
    """每块物理盘一个 problem 二值传感器。"""
    storage = _storage("10.18.176.64")
    ents = bs_mod._build_per_hdd_binary_entities(_Coord(_data(storage)), _entry())
    uids = [e._attr_unique_id for e in ents]
    for hid in ("1", "2", "4", "5"):
        assert any(f"hdd_{hid}_problem" in u for u in uids), f"缺 hdd_{hid}_problem"
    # 空槽不建
    assert not any("hdd_3_problem" in u for u in uids)


def test_per_hdd_problem_off_when_ok():
    """ok 盘 → problem = False。"""
    storage = _storage("10.18.176.64")
    ents = bs_mod._build_per_hdd_binary_entities(_Coord(_data(storage)), _entry())
    e = next(e for e in ents if "hdd_1_problem" in e._attr_unique_id)
    assert e.is_on is False


def test_per_hdd_problem_on_when_error():
    """error 盘 → problem = True。"""
    xml = """<storage version="2.0"><hddList>
      <hdd><id>1</id><hddName>hdd1</hddName><status>error</status>
        <capacity>1000</capacity><freeSpace>0</freeSpace></hdd>
      </hddList></storage>"""
    storage = _parse_storage(ET.fromstring(xml))
    ents = bs_mod._build_per_hdd_binary_entities(_Coord(_data(storage)), _entry())
    e = next(e for e in ents if "hdd_1_problem" in e._attr_unique_id)
    assert e.is_on is True


def test_per_hdd_problem_device_class():
    """problem 二值传感器用 PROBLEM device class（HA 渲染为红色）。"""
    storage = _storage("10.18.176.65")
    ents = bs_mod._build_per_hdd_binary_entities(_Coord(_data(storage)), _entry())
    e = ents[0]
    assert e._attr_device_class == "problem"


def test_ipc_without_disks_no_problem_entities():
    """IPC 无盘 → 不产生 problem 实体。"""
    storage = _storage("10.18.176.18")
    ents = bs_mod._build_per_hdd_binary_entities(_Coord(_data(storage)), _entry())
    assert ents == []


def test_per_hdd_problem_idle_is_not_problem():
    """idle（备用盘）是正常态 → problem = False。"""
    xml = """<storage version="2.0"><hddList>
      <hdd><id>1</id><hddName>hdd1</hddName><status>idle</status>
        <capacity>1000</capacity><freeSpace>0</freeSpace></hdd>
      </hddList></storage>"""
    storage = _parse_storage(ET.fromstring(xml))
    ents = bs_mod._build_per_hdd_binary_entities(_Coord(_data(storage)), _entry())
    e = next(e for e in ents if "hdd_1_problem" in e._attr_unique_id)
    assert e.is_on is False


# ── 接线测试：驱动真实 async_setup_entry ────────────────────────────
#
# 上面所有测试只验证了生成器函数本身。但生成器存在 ≠ 实体会出现在 HA。
# v0.7.3 曾出现"监听器写成 async def，HA 同步调用丢弃返回值，函数体
# 永不执行"的缺陷。以下测试模拟 HA 的真实调用语义，确认：
#   1. storage 数据迟到时，逐盘实体确实被注册（sensor + binary 两平台）
#   2. 所有监听器都是同步函数，不产生没人 await 的协程
#   3. 无盘设备不注册任何盘实体


class _FakeCoordinator:
    """忠实模拟 DataUpdateCoordinator 的监听器语义。

    ``fire()`` 按 HA 的方式同步调用监听器并丢弃返回值；返回协程则
    说明该监听器被写成了 async def（其函数体永不执行），记录下来供断言。
    """

    def __init__(self, *, device_type="", storage=None, data=None):
        self.device_type = device_type
        self.channels: list[dict] = []
        self.network_interfaces: list[dict] = []
        self.device_info: dict = {}
        self.storage = storage or {}
        self.data = data
        self.capabilities: dict = {}
        self.streaming_channel_detail: dict = {}
        self.unique_id = f"{DOMAIN}_wire"
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


def _hass(coordinator):
    h = type("H", (), {})()
    h.data = {DOMAIN: {"wire_entry": coordinator}}
    return h


def _wire_entry():
    e = type("E", (), {})()
    e.entry_id = "wire_entry"
    e.data = {"host": "10.18.176.64"}
    e.options = {}
    return e


@pytest.mark.asyncio
async def test_sensor_setup_registers_hdd_when_storage_arrives_late():
    """storage 迟到：sensor 平台必须在监听器触发后注册逐盘实体。"""
    coord = _FakeCoordinator(device_type="")  # setup 时 device_type 未知
    added: list = []
    await sensor_mod.async_setup_entry(_hass(coord), _wire_entry(), added.extend)

    # 监听器不得是 async def
    offenders = [getattr(f, "__name__", repr(f)) for f in coord._listeners
                 if inspect.iscoroutinefunction(f)]
    assert not offenders, f"async def 监听器函数体永不执行: {offenders}"

    # setup 时无 storage 数据 → 无盘实体
    assert not any(getattr(e, "entity_description", None) and
                   e.entity_description.key.startswith("hdd_") for e in added)
    assert not coord.unawaited_coroutines

    # 首次刷新：NVR + 4 块盘
    coord.device_type = "networkvideorecorder"
    coord.storage = _storage("10.18.176.64")
    coord.data = _data(coord.storage)
    coord.data.device_info["deviceType"] = "NVR"
    coord.device_info = coord.data.device_info
    coord.fire()

    assert not coord.unawaited_coroutines, "监听器返回了协程 → 函数体未执行"
    hdd_keys = {e.entity_description.key for e in added
                if getattr(e, "entity_description", None)}
    assert "hdd_1_status" in hdd_keys, "逐盘传感器应在 storage 到达后注册"
    assert "hdd_error_count" in hdd_keys


@pytest.mark.asyncio
async def test_binary_setup_registers_hdd_problem_when_storage_arrives_late():
    """storage 迟到：binary 平台必须在监听器触发后注册逐盘 problem 实体。"""
    coord = _FakeCoordinator(device_type="")
    added: list = []
    await bs_mod.async_setup_entry(_hass(coord), _wire_entry(), added.extend)

    offenders = [getattr(f, "__name__", repr(f)) for f in coord._listeners
                 if inspect.iscoroutinefunction(f)]
    assert not offenders, f"async def 监听器函数体永不执行: {offenders}"

    coord.device_type = "networkvideorecorder"
    coord.storage = _storage("10.18.176.64")
    coord.data = _data(coord.storage)
    coord.fire()

    assert not coord.unawaited_coroutines
    uids = [e._attr_unique_id for e in added]
    assert any("hdd_1_problem" in u for u in uids), "逐盘 problem 应在 storage 到达后注册"


@pytest.mark.asyncio
async def test_no_hdd_entities_for_ipc():
    """IPC（无盘）：即使触发监听器也不得注册任何盘实体。"""
    coord = _FakeCoordinator(device_type="")
    added: list = []
    await sensor_mod.async_setup_entry(_hass(coord), _wire_entry(), added.extend)
    await bs_mod.async_setup_entry(_hass(coord), _wire_entry(), added.extend)

    coord.device_type = "ipcamera"
    coord.storage = _storage("10.18.176.18")  # hdds == []
    coord.data = _data(coord.storage)
    coord.fire()

    hdd = [e for e in added
           if (getattr(e, "entity_description", None) and
               e.entity_description.key.startswith("hdd_"))
           or "hdd_" in getattr(e, "_attr_unique_id", "")]
    assert hdd == [], f"IPC 不应有盘实体，实得 {len(hdd)} 个"

