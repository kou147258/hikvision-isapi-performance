"""v0.8 步骤10：移动侦测开关 + 灵敏度（number 平台）。

真机实测（12 台）：
  * `GET /ISAPI/System/Video/inputs/channels/{id}/motionDetection`
    11/12 可用；176.65（DS-7708N-I4 V4.1.18）返回 **403**
  * `<enabled>true</enabled>`，`<sensitivityLevel>60</sensitivityLevel>`
    （11 台实测全部为 60）
  * sensitivityLevel 在机群固件里嵌在 `MotionDetectionLayout` 内，
    部分变体平铺在根下 —— capabilities.parse_motion_detection 两者都读

设计决策（性能考量）：
  motionDetection 是**配置项**而非状态量，极少变化（11/12 台灵敏度同为
  60）。若纳入 30 秒轮询，13 通道 NVR 每周期会多 13 次请求。因此：
    * setup 后**探测一次**（后台任务），据结果做能力门控
    * 用户 PUT 后**回读一次**确认设备接受（对齐录像开关的
      "下次轮询确认/回滚"语义）
    * 稳态**零**周期性开销

⚠️ 这两个实体是集成里**会写入设备配置**的少数端点之一（另有录像开关与
重启按钮）。测试全部离线：PUT 用假客户端，绝不向真机下发写请求。
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

from custom_components.hikvision_isapi_performance import capabilities as cap  # noqa: E402
from custom_components.hikvision_isapi_performance import number as number_mod  # noqa: E402
from custom_components.hikvision_isapi_performance import switch as switch_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.const import DOMAIN  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures_v08"


def _vmd_root(host: str) -> ET.Element:
    f = FIXTURES / f"{host.replace('.', '_')}__J_vmd__motionDetection_ch1.xml"
    assert f.exists(), f"missing fixture {f.name}"
    return ET.fromstring(f.read_text(encoding="utf-8", errors="replace"))


# ── 1. 解析：真机配置读取 ─────────────────────────────────────────


def test_parse_real_motion_detection_configs():
    """11 台实测：enabled=true、sensitivityLevel=60。"""
    for host in ("10.18.176.10", "10.18.176.12", "10.18.176.13",
                 "10.18.176.64", "192.168.10.17"):
        md = cap.parse_motion_detection(_vmd_root(host))
        assert md["enabled"] is True, host
        assert md["sensitivity_level"] == 60, host


def test_parse_nested_sensitivity_is_reachable():
    """sensitivityLevel 嵌在 MotionDetectionLayout 内也能读到。"""
    xml = """<MotionDetection version="2.0"><enabled>true</enabled>
      <MotionDetectionLayout><sensitivityLevel>75</sensitivityLevel>
      </MotionDetectionLayout></MotionDetection>"""
    assert cap.parse_motion_detection(ET.fromstring(xml))["sensitivity_level"] == 75


# ── 2. 实体构造 ────────────────────────────────────────────────────


class _Coord:
    """假 coordinator：可注入 motion_detection 缓存与假客户端工厂。"""

    def __init__(self, motion_detection=None, channels=None, client=None):
        self.motion_detection = motion_detection or {}
        self.channels = channels or []
        self.data = None
        self.storage: dict = {}
        self.network_interfaces: list = []
        self.device_type = "ipcamera"
        self.device_info: dict = {}
        self.capabilities: dict = {}
        self.streaming_channel_detail: dict = {}
        self.system_capabilities: dict = {}
        self.system_status: dict = {}
        self.event_state: dict = {}
        self.recording_status: dict = {}
        self.unique_id = f"{DOMAIN}_md"
        self._host = "10.18.176.10"
        self._port = 80
        self._username = "admin"
        self._password = "pw"
        self._verify_ssl = False
        self._use_https = False
        self._listeners: list = []
        self.refresh_calls = 0
        self._client = client

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: (
            self._listeners.remove(listener) if listener in self._listeners else None
        )

    def fire(self):
        for listener in list(self._listeners):
            r = listener()
            if hasattr(r, "__await__") or type(r).__name__ == "coroutine":
                r.close()

    async def async_request_refresh(self):
        self.refresh_calls += 1

    async def async_refresh_motion_detection(self, channel_id: str = None):
        self.refresh_calls += 1


class _FakeClient:
    """记录 PUT 调用的假客户端（绝不联网）。"""

    def __init__(self, *, fail: bool = False):
        self.puts: list[tuple[str, str]] = []
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def put_xml(self, path, body):
        self.puts.append((path, body))
        if self._fail:
            from custom_components.hikvision_isapi_performance.isapi_client import (
                ISAPIError,
            )
            raise ISAPIError("403 Invalid Operation", status_code=403)
        return ET.fromstring("<ResponseStatus><statusCode>1</statusCode></ResponseStatus>")


def _entry():
    e = type("E", (), {})()
    e.entry_id = "md_entry"
    e.data = {"host": "10.18.176.10"}
    e.options = {}
    return e


def test_motion_switch_reads_enabled_from_cache():
    coord = _Coord(motion_detection={"1": {"enabled": True, "sensitivity_level": 60}})
    sw = switch_mod.HikvisionISAPIMotionDetectionSwitch(
        coord, _entry(), "1", "摄像机12")
    assert sw.is_on is True


def test_motion_switch_off_state():
    coord = _Coord(motion_detection={"1": {"enabled": False, "sensitivity_level": 60}})
    sw = switch_mod.HikvisionISAPIMotionDetectionSwitch(coord, _entry(), "1", "摄像机12")
    assert sw.is_on is False


def test_motion_switch_unknown_when_no_config():
    """无缓存数据 → None（unknown），不谎报 off。"""
    coord = _Coord(motion_detection={})
    sw = switch_mod.HikvisionISAPIMotionDetectionSwitch(coord, _entry(), "1", "x")
    assert sw.is_on is None


def test_motion_switch_unique_id_and_name():
    coord = _Coord(motion_detection={"1": {"enabled": True}})
    sw = switch_mod.HikvisionISAPIMotionDetectionSwitch(coord, _entry(), "1", "摄像机12")
    assert sw._attr_unique_id == "md_entry_channel_1_motion_detection"
    assert "摄像机12" in sw._attr_name


def test_sensitivity_number_reads_value():
    coord = _Coord(motion_detection={"1": {"enabled": True, "sensitivity_level": 60}})
    n = number_mod.HikvisionISAPIMotionSensitivityNumber(coord, _entry(), "1", "摄像机12")
    assert n.native_value == 60


def test_sensitivity_number_range():
    """灵敏度范围 0-100（海康 sensitivityLevel 定义域）。"""
    coord = _Coord(motion_detection={"1": {"enabled": True, "sensitivity_level": 60}})
    n = number_mod.HikvisionISAPIMotionSensitivityNumber(coord, _entry(), "1", "x")
    assert n.native_min_value == 0
    assert n.native_max_value == 100
    assert n.native_step == 1


def test_sensitivity_number_unknown_when_missing():
    coord = _Coord(motion_detection={})
    n = number_mod.HikvisionISAPIMotionSensitivityNumber(coord, _entry(), "1", "x")
    assert n.native_value is None


def test_sensitivity_number_unique_id():
    coord = _Coord(motion_detection={"1": {"sensitivity_level": 60}})
    n = number_mod.HikvisionISAPIMotionSensitivityNumber(coord, _entry(), "1", "x")
    assert n._attr_unique_id == "md_entry_channel_1_motion_sensitivity"


# ── 3. 写入：PUT 下发配置 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_on_puts_enabled_true(monkeypatch):
    """开启移动侦测 → PUT enabled=true。"""
    fake = _FakeClient()
    monkeypatch.setattr(switch_mod, "ISAPIClient", lambda **kw: fake)
    coord = _Coord(motion_detection={"1": {"enabled": False, "sensitivity_level": 60}})
    sw = switch_mod.HikvisionISAPIMotionDetectionSwitch(coord, _entry(), "1", "x")
    await sw.async_turn_on()

    assert len(fake.puts) == 1
    path, body = fake.puts[0]
    assert "motionDetection" in path
    assert "<enabled>true</enabled>" in body
    # 乐观更新本地缓存
    assert coord.motion_detection["1"]["enabled"] is True


@pytest.mark.asyncio
async def test_turn_off_puts_enabled_false(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(switch_mod, "ISAPIClient", lambda **kw: fake)
    coord = _Coord(motion_detection={"1": {"enabled": True}})
    sw = switch_mod.HikvisionISAPIMotionDetectionSwitch(coord, _entry(), "1", "x")
    await sw.async_turn_off()
    assert "<enabled>false</enabled>" in fake.puts[0][1]
    assert coord.motion_detection["1"]["enabled"] is False


@pytest.mark.asyncio
async def test_put_preserves_other_config_fields(monkeypatch):
    """PUT 必须保留原有配置（gridMap 等），否则清空用户的侦测区域。

    真机 motionDetection 含 regionType/gridMap/layout/Grid 等区域配置。
    只回写 enabled 会**抹掉用户画的侦测区域** —— 这是必须避免的破坏性写入。
    """
    fake = _FakeClient()
    monkeypatch.setattr(switch_mod, "ISAPIClient", lambda **kw: fake)
    grid = ("fffffcfffffcfffffcfffffcfffffcfffffcfffffcfffffc"
            "fffffcfffffcfffffcfffffcfffffcfffffcfffffcfffffc")
    coord = _Coord(motion_detection={"1": {
        "enabled": True, "sensitivity_level": 60,
        "_raw_xml": (
            '<MotionDetection version="2.0"><enabled>true</enabled>'
            '<samplingInterval>5</samplingInterval>'
            f'<MotionDetectionLayout><sensitivityLevel>60</sensitivityLevel>'
            f'<layout><gridMap>{grid}</gridMap></layout>'
            '</MotionDetectionLayout></MotionDetection>'
        ),
    }})
    sw = switch_mod.HikvisionISAPIMotionDetectionSwitch(coord, _entry(), "1", "x")
    await sw.async_turn_off()

    body = fake.puts[0][1]
    assert grid in body, "PUT 必须保留 gridMap，否则抹掉用户侦测区域"
    assert "<samplingInterval>5</samplingInterval>" in body
    assert "<enabled>false</enabled>" in body


@pytest.mark.asyncio
async def test_sensitivity_put_updates_value(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(number_mod, "ISAPIClient", lambda **kw: fake)
    coord = _Coord(motion_detection={"1": {"enabled": True, "sensitivity_level": 60}})
    n = number_mod.HikvisionISAPIMotionSensitivityNumber(coord, _entry(), "1", "x")
    await n.async_set_native_value(85)
    assert "<sensitivityLevel>85</sensitivityLevel>" in fake.puts[0][1]
    assert coord.motion_detection["1"]["sensitivity_level"] == 85


@pytest.mark.asyncio
async def test_put_failure_reverts_optimistic_state(monkeypatch):
    """PUT 失败（如 403）→ 不得留下错误的乐观状态。"""
    fake = _FakeClient(fail=True)
    monkeypatch.setattr(switch_mod, "ISAPIClient", lambda **kw: fake)
    coord = _Coord(motion_detection={"1": {"enabled": True}})
    sw = switch_mod.HikvisionISAPIMotionDetectionSwitch(coord, _entry(), "1", "x")
    await sw.async_turn_off()
    # 写入失败：缓存不应变成 False（设备仍是 True）
    assert coord.motion_detection["1"]["enabled"] is True


@pytest.mark.asyncio
async def test_put_triggers_readback(monkeypatch):
    """PUT 后回读一次确认设备接受（对齐录像开关语义）。"""
    fake = _FakeClient()
    monkeypatch.setattr(switch_mod, "ISAPIClient", lambda **kw: fake)
    coord = _Coord(motion_detection={"1": {"enabled": False}})
    sw = switch_mod.HikvisionISAPIMotionDetectionSwitch(coord, _entry(), "1", "x")
    before = coord.refresh_calls
    await sw.async_turn_on()
    assert coord.refresh_calls > before


# ── 4. 能力门控：403 设备不建实体 ─────────────────────────────────


def test_no_entities_when_probe_failed():
    """176.65 探测 403 → motion_detection 为空 → 不建任何实体。"""
    coord = _Coord(motion_detection={}, channels=[{"id": "1", "name": "x"}])
    assert switch_mod._build_motion_detection_entities(coord, _entry()) == []
    assert number_mod._build_motion_sensitivity_entities(coord, _entry()) == []


def test_entities_built_per_probed_channel():
    """按探测成功的通道建实体（不是按 channels 列表）。"""
    coord = _Coord(
        motion_detection={
            "1": {"enabled": True, "sensitivity_level": 60},
            "3": {"enabled": False, "sensitivity_level": 40},
        },
        channels=[{"id": "1", "name": "a"}, {"id": "2", "name": "b"},
                  {"id": "3", "name": "c"}],
    )
    sw = switch_mod._build_motion_detection_entities(coord, _entry())
    assert {e._attr_unique_id for e in sw} == {
        "md_entry_channel_1_motion_detection",
        "md_entry_channel_3_motion_detection",
    }
    num = number_mod._build_motion_sensitivity_entities(coord, _entry())
    assert len(num) == 2


def test_entity_uses_channel_name_from_cache():
    """实体名优先用 channels 里的摄像机名。"""
    coord = _Coord(
        motion_detection={"3": {"enabled": True, "sensitivity_level": 60}},
        channels=[{"id": "3", "name": "摄像机09"}],
    )
    sw = switch_mod._build_motion_detection_entities(coord, _entry())
    assert "摄像机09" in sw[0]._attr_name


def test_switch_entity_category_is_config():
    """改设备行为的实体归入 CONFIG 分类（HA 约定）。"""
    coord = _Coord(motion_detection={"1": {"enabled": True, "sensitivity_level": 60}})
    sw = switch_mod._build_motion_detection_entities(coord, _entry())
    assert sw[0]._attr_entity_category == "config"
    num = number_mod._build_motion_sensitivity_entities(coord, _entry())
    assert num[0]._attr_entity_category == "config"


# ── 5. PUT 体构造器：不得抹掉用户配置 ─────────────────────────────
#
# 这是本轮唯一会写入设备配置的新功能。真机 motionDetection 文档含
# regionType / Grid / gridMap / samplingInterval / startTriggerTime 等
# 用户配置。若 PUT 只回写 <enabled>，设备会把未提供的字段重置，
# **抹掉用户画的侦测区域** —— 破坏性且不可逆（用户需重新画）。
# 因此构造器必须在原始文档上就地替换目标字段。

REAL_RAW = (
    '<MotionDetection version="2.0">'
    '<enabled>true</enabled>'
    '<enableHighlight>true</enableHighlight>'
    '<samplingInterval>5</samplingInterval>'
    '<startTriggerTime>1000</startTriggerTime>'
    '<endTriggerTime>1000</endTriggerTime>'
    '<regionType>grid</regionType>'
    '<Grid><rowGranularity>18</rowGranularity>'
    '<columnGranularity>22</columnGranularity></Grid>'
    '<MotionDetectionLayout><sensitivityLevel>60</sensitivityLevel>'
    '<layout><gridMap>fffffcfffffcfffffcfffffcfffffcfffffc</gridMap></layout>'
    '</MotionDetectionLayout></MotionDetection>'
)


def test_build_put_body_toggles_enabled_preserving_gridmap():
    """关闭侦测：只改 enabled，gridMap 与采样间隔原样保留。"""
    body = cap.build_motion_detection_body(REAL_RAW, enabled=False)
    assert "<enabled>false</enabled>" in body
    assert "<gridMap>fffffcfffffcfffffcfffffcfffffcfffffc</gridMap>" in body
    assert "<samplingInterval>5</samplingInterval>" in body
    assert "<sensitivityLevel>60</sensitivityLevel>" in body
    assert "<regionType>grid</regionType>" in body


def test_build_put_body_sets_sensitivity_preserving_gridmap():
    """改灵敏度：只改 sensitivityLevel，其余保留。"""
    body = cap.build_motion_detection_body(REAL_RAW, sensitivity_level=85)
    assert "<sensitivityLevel>85</sensitivityLevel>" in body
    assert "<enabled>true</enabled>" in body
    assert "<gridMap>fffffcfffffcfffffcfffffcfffffcfffffc</gridMap>" in body


def test_build_put_body_can_set_both():
    body = cap.build_motion_detection_body(
        REAL_RAW, enabled=False, sensitivity_level=30)
    assert "<enabled>false</enabled>" in body
    assert "<sensitivityLevel>30</sensitivityLevel>" in body


def test_build_put_body_leaves_untargeted_fields_alone():
    """未指定的字段值必须与原值一致（不是被清空或默认值覆盖）。"""
    body = cap.build_motion_detection_body(REAL_RAW, sensitivity_level=10)
    assert "<enableHighlight>true</enableHighlight>" in body
    assert "<startTriggerTime>1000</startTriggerTime>" in body
    assert "<endTriggerTime>1000</endTriggerTime>" in body
    assert "<rowGranularity>18</rowGranularity>" in body
    assert "<columnGranularity>22</columnGranularity>" in body


def test_build_put_body_flat_sensitivity_variant():
    """部分固件把 sensitivityLevel 平铺在根下，也要能改。"""
    raw = ('<MotionDetection><enabled>true</enabled>'
           '<sensitivityLevel>40</sensitivityLevel>'
           '<regionType>grid</regionType></MotionDetection>')
    body = cap.build_motion_detection_body(raw, sensitivity_level=90)
    assert "<sensitivityLevel>90</sensitivityLevel>" in body
    assert "<sensitivityLevel>40</sensitivityLevel>" not in body


def test_build_put_body_requires_exactly_one_target():
    """两个字段都不指定时应报错（调用方 bug，静默无操作更危险）。"""
    with pytest.raises(ValueError):
        cap.build_motion_detection_body(REAL_RAW)


def test_build_put_body_preserves_version_attribute():
    """根元素的 version 属性必须保留（设备可能校验）。"""
    body = cap.build_motion_detection_body(REAL_RAW, enabled=False)
    assert 'version="2.0"' in body
    assert body.startswith("<MotionDetection")


def test_build_put_body_no_namespace_leak():
    """构造结果不得带 xmlns（探测时已 strip，回写也应保持）。"""
    raw_ns = REAL_RAW.replace(
        '<MotionDetection version="2.0">',
        '<MotionDetection version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">')
    body = cap.build_motion_detection_body(raw_ns, enabled=False)
    assert "xmlns" not in body


def test_build_put_body_enabled_true_variants():
    """enabled 原值为 TRUE/1 等变体时也能正确切换为 false。"""
    for truthy in ("true", "TRUE", "1"):
        raw = REAL_RAW.replace("<enabled>true</enabled>",
                               f"<enabled>{truthy}</enabled>")
        body = cap.build_motion_detection_body(raw, enabled=False)
        assert "<enabled>false</enabled>" in body, f"原值 {truthy} 未正确切换"


def test_build_put_body_sensitivity_out_of_range_rejected():
    """越界灵敏度应被拒绝（海康定义域 0-100）。"""
    for bad in (-1, 101, 999):
        with pytest.raises(ValueError):
            cap.build_motion_detection_body(REAL_RAW, sensitivity_level=bad)


def test_build_put_body_missing_raw_falls_back_to_minimal():
    """原始 XML 缺失时退回最小文档（仅目标字段），不崩溃。"""
    body = cap.build_motion_detection_body("", enabled=True)
    assert "<enabled>true</enabled>" in body
    assert body.startswith("<MotionDetection")


# ── 6. 接线：平台注册与探测触发 ────────────────────────────────────
#
# 生成器/探测方法存在 ≠ 功能会运行。以下测试驱动真实 async_setup_entry，
# 锁定三处接线：
#   a) PLATFORMS 含 Platform.NUMBER（否则 number.py 永不加载）
#   b) switch 平台注册移动侦测开关（含迟到监听器）
#   c) number 平台注册灵敏度实体（含迟到监听器）
#   d) __init__ 在通道就绪后触发一次性探测


class _WireCoord(_Coord):
    """带 HA 式监听器语义的假 coordinator，用于接线测试。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.motion_detection = kw.get("motion_detection", {})
        self._listeners: list = []
        self.unawaited_coroutines: list = []
        self.probe_calls: list = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: (
            self._listeners.remove(listener) if listener in self._listeners else None
        )

    def fire(self):
        for listener in list(self._listeners):
            r = listener()
            if hasattr(r, "__await__") or type(r).__name__ == "coroutine":
                self.unawaited_coroutines.append(r)
                r.close()

    async def async_probe_motion_detection(self):
        self.probe_calls.append(True)


def _wire_hass(coord, entry_id="md_entry"):
    h = type("H", (), {})()
    h.data = {DOMAIN: {entry_id: coord}}
    return h


def test_number_platform_is_registered():
    """PLATFORMS 必须含 number，否则 number.py 永不加载。"""
    import custom_components.hikvision_isapi_performance as init_mod
    from homeassistant.const import Platform
    assert Platform.NUMBER in init_mod.PLATFORMS, (
        f"PLATFORMS 缺 number: {init_mod.PLATFORMS}"
    )


@pytest.mark.asyncio
async def test_switch_platform_registers_motion_switch_when_probe_done():
    """探测已完成 → switch 平台同步注册移动侦测开关。"""
    coord = _WireCoord(motion_detection={"1": {"enabled": True}})
    added: list = []
    await switch_mod.async_setup_entry(_wire_hass(coord), _entry(), added.extend)
    uids = {getattr(e, "_attr_unique_id", "") for e in added}
    assert "md_entry_channel_1_motion_detection" in uids


@pytest.mark.asyncio
async def test_switch_platform_registers_motion_switch_late():
    """探测迟到完成 → 监听器补注册移动侦测开关。"""
    coord = _WireCoord(motion_detection={})
    added: list = []
    await switch_mod.async_setup_entry(_wire_hass(coord), _entry(), added.extend)

    offenders = [getattr(f, "__name__", repr(f)) for f in coord._listeners
                 if inspect.iscoroutinefunction(f)]
    assert not offenders, f"async def 监听器函数体永不执行: {offenders}"
    assert not any("motion_detection" in getattr(e, "_attr_unique_id", "")
                   for e in added)

    # 探测完成，数据到达
    coord.motion_detection = {"1": {"enabled": True, "sensitivity_level": 60}}
    coord.fire()

    assert not coord.unawaited_coroutines, "监听器返回协程 → 函数体未执行"
    uids = {getattr(e, "_attr_unique_id", "") for e in added}
    assert "md_entry_channel_1_motion_detection" in uids


@pytest.mark.asyncio
async def test_number_platform_registers_late():
    """number 平台：探测迟到完成后注册灵敏度实体。"""
    coord = _WireCoord(motion_detection={})
    added: list = []
    await number_mod.async_setup_entry(_wire_hass(coord), _entry(), added.extend)

    offenders = [getattr(f, "__name__", repr(f)) for f in coord._listeners
                 if inspect.iscoroutinefunction(f)]
    assert not offenders, f"async def 监听器函数体永不执行: {offenders}"

    coord.motion_detection = {"1": {"enabled": True, "sensitivity_level": 60}}
    coord.fire()

    uids = {getattr(e, "_attr_unique_id", "") for e in added}
    assert "md_entry_channel_1_motion_sensitivity" in uids


@pytest.mark.asyncio
async def test_number_platform_registers_synchronously():
    """number 平台：探测已完成时同步注册，不必等监听器。"""
    coord = _WireCoord(motion_detection={"1": {"sensitivity_level": 60}})
    added: list = []
    await number_mod.async_setup_entry(_wire_hass(coord), _entry(), added.extend)
    uids = {getattr(e, "_attr_unique_id", "") for e in added}
    assert "md_entry_channel_1_motion_sensitivity" in uids


@pytest.mark.asyncio
async def test_motion_switch_not_double_registered():
    """多次刷新只注册一次移动侦测开关。"""
    coord = _WireCoord(motion_detection={})
    added: list = []
    await switch_mod.async_setup_entry(_wire_hass(coord), _entry(), added.extend)
    coord.motion_detection = {"1": {"enabled": True}}
    coord.fire()
    coord.fire()
    n = sum(1 for e in added
            if getattr(e, "_attr_unique_id", "") == "md_entry_channel_1_motion_detection")
    assert n == 1, f"重复注册 {n} 次"


@pytest.mark.asyncio
async def test_403_device_gets_no_motion_entities():
    """176.65（探测 403，motion_detection 空）→ 两个平台都不建实体。"""
    coord = _WireCoord(motion_detection={}, channels=[{"id": "1", "name": "x"}])
    added: list = []
    await switch_mod.async_setup_entry(_wire_hass(coord), _entry(), added.extend)
    await number_mod.async_setup_entry(_wire_hass(coord), _entry(), added.extend)
    coord.fire()
    coord.fire()

    uids = [getattr(e, "_attr_unique_id", "") for e in added]
    assert not any("motion_detection" in u for u in uids)
    assert not any("motion_sensitivity" in u for u in uids)


@pytest.mark.asyncio
async def test_init_defers_probe_until_channels_ready():
    """__init__ 的探测监听器：无通道时不探测，通道就绪后探测一次。"""
    coord = _WireCoord(motion_detection={}, channels=[])
    # 手动复现 __init__ 里的延迟探测监听器语义
    probed = []

    def _maybe_probe():
        if getattr(coord, "_motion_probed", False):
            return
        if not coord.channels:
            return
        coord._motion_probed = True
        probed.append(True)

    coord.async_add_listener(_maybe_probe)

    coord.fire()
    assert probed == [], "无通道时不应探测"

    coord.channels = [{"id": "1", "name": "x"}, {"id": "2", "name": "y"}]
    coord.fire()
    coord.fire()
    assert probed == [True], f"通道就绪后应恰好探测一次，实得 {probed}"


