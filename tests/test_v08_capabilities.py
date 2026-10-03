"""v0.8 步骤4：capabilities.py — 实体级能力门控（纯解析，无网络）。

职责：回答"这台设备到底提供哪些数据"，让各平台**只创建有数据源的实体**，
消除永久显示"未知"的噪音实体（用户已确认此方向）。

与 coordinator._parse_capabilities 的区别：
  * _parse_capabilities 解析 /System/capabilities，用途是**设备分类**
    （IPC/NVR/DVR 交叉校验、通道数校验）
  * 本模块是**实体级门控**：reboot_count 该不该建、录像检索能不能用、
    移动侦测有没有配置、支持哪些事件类型

全部函数为纯函数（输入 XML/dict，输出结构化结果），不发网络请求，
因此可用真机抓包做 fixture 完整覆盖。

真机实测依据（12 台机群）：
  * totalRebootCount: 6/12 台返回，6/12 台无该字段
  * motionDetection:  11/12 台可用（176.65 返回 403）
  * Event/triggers:    9/12 台可用
  * InputProxyChannelList 的 size 属性不可信（176.64 声明 19 实际 13，
    176.65 声明 0 实际 8）→ 必须按通道块计数
  * trackID = {通道号}01；越界 trackID 在部分设备导致整体 400，
    因此必须用精确通道数生成
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import capabilities as cap  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures_v08"


def _root(prefix: str) -> ET.Element:
    files = sorted(FIXTURES.glob(prefix + "*.xml"))
    assert files, f"missing fixture for {prefix}"
    return ET.fromstring(files[0].read_text(encoding="utf-8", errors="replace"))


def _status_root(host: str) -> ET.Element:
    return _root(f"{host.replace('.', '_')}__B_reboot__ISAPI_System_status")


def _proxy_root(host: str) -> ET.Element:
    return _root(f"{host.replace('.', '_')}__I_proxy__")


def _vmd_root(host: str) -> ET.Element:
    return _root(f"{host.replace('.', '_')}__J_vmd__")


def _trig_root(host: str) -> ET.Element:
    return _root(f"{host.replace('.', '_')}__E_trig__")


# ── 1. reboot_count 门控 ────────────────────────────────────────────


def test_has_reboot_count_true_for_devices_that_report_it():
    """实测返回 totalRebootCount 的 6 台 → True。"""
    for host in ("10.18.176.10", "10.18.176.12", "10.18.176.13",
                 "10.18.176.51", "10.18.176.52", "10.18.176.53"):
        assert cap.has_reboot_count(_status_root(host)) is True, host


def test_has_reboot_count_false_when_field_absent():
    """实测无该字段的 6 台 → False（含全部 3 台 NVR）。

    关键：值为 0 与字段缺失必须区分。176.13/51/52/53 的
    totalRebootCount=0 是"确实重启过 0 次"，必须建实体；
    而 NVR 根本没有该字段，建了就是永久 unknown。
    """
    for host in ("10.18.176.16", "10.18.176.17", "10.18.176.18",
                 "10.18.176.64", "10.18.176.65", "192.168.10.17"):
        assert cap.has_reboot_count(_status_root(host)) is False, host


def test_has_reboot_count_distinguishes_zero_from_absent():
    """值为 0 → True（建实体）；字段缺失 → False（不建）。"""
    assert cap.has_reboot_count(_status_root("10.18.176.13")) is True  # =0
    assert cap.has_reboot_count(_status_root("10.18.176.64")) is False  # 缺失
    # 显式构造：0 与缺失
    zero = ET.fromstring("<DeviceStatus><totalRebootCount>0</totalRebootCount></DeviceStatus>")
    absent = ET.fromstring("<DeviceStatus><deviceUpTime>5</deviceUpTime></DeviceStatus>")
    assert cap.has_reboot_count(zero) is True
    assert cap.has_reboot_count(absent) is False


def test_has_reboot_count_none_root():
    assert cap.has_reboot_count(None) is False


def test_reboot_count_value_parsed():
    """字段存在时返回整数值。"""
    assert cap.reboot_count_value(_status_root("10.18.176.10")) == 41
    assert cap.reboot_count_value(_status_root("10.18.176.12")) == 1
    assert cap.reboot_count_value(_status_root("10.18.176.13")) == 0
    assert cap.reboot_count_value(_status_root("10.18.176.64")) is None


# ── 2. 通道数：size 属性不可信（回归钉子）─────────────────────────


def test_channel_count_ignores_unreliable_size_attribute():
    """必须按通道块计数，不得信任 size 属性。

    实测：176.64 声明 size="19" 实际 13 块；176.65 声明 size="0"
    实际 8 块。信任 size 会让 176.65 变成 0 通道。
    """
    assert cap.count_proxy_channels(_proxy_root("10.18.176.64")) == 13
    assert cap.count_proxy_channels(_proxy_root("10.18.176.65")) == 8
    assert cap.count_proxy_channels(_proxy_root("192.168.10.17")) == 2


def test_channel_count_matches_attribute_mismatch_evidence():
    """固化"声明 size 与实际不符"这一实测事实，防止将来改用 size。"""
    root64 = _proxy_root("10.18.176.64")
    root65 = _proxy_root("10.18.176.65")
    declared64 = root64.attrib.get("size")
    declared65 = root65.attrib.get("size")
    assert declared64 == "19" and cap.count_proxy_channels(root64) == 13
    assert declared65 == "0" and cap.count_proxy_channels(root65) == 8


def test_channel_count_matches_versioned_open_tags():
    """开标签可能带 version 属性，计数必须能匹配（探测脚本曾因此漏计）。

    176.65/192.168.10.17 用 <InputProxyChannel version="1.0">，
    176.64 用 <InputProxyChannel>。
    """
    assert cap.count_proxy_channels(_proxy_root("10.18.176.65")) == 8
    xml_plain = "<InputProxyChannelList size='0'><InputProxyChannel version='1.0'><id>1</id></InputProxyChannel></InputProxyChannelList>"
    assert cap.count_proxy_channels(ET.fromstring(xml_plain)) == 1


def test_channel_count_empty_and_none():
    assert cap.count_proxy_channels(None) == 0
    assert cap.count_proxy_channels(ET.fromstring("<InputProxyChannelList/>")) == 0


# ── 3. trackID 生成（录像检索前提）────────────────────────────────


def test_track_ids_follow_channel_stream_convention():
    """trackID = {通道号}01。实测 176.64 的 101..1301 全部有效。"""
    assert cap.track_ids(3) == ["101", "201", "301"]
    assert cap.track_ids(1) == ["101"]
    assert cap.track_ids(13) == [f"{i}01" for i in range(1, 14)]
    assert cap.track_ids(13)[-1] == "1301"


def test_track_ids_zero_or_negative():
    """无通道 → 空列表（不发无效请求）。"""
    assert cap.track_ids(0) == []
    assert cap.track_ids(-1) == []


def test_search_max_results_scales_with_channels():
    """maxResults 必须 >= 通道数×2，否则 MORE 截断会漏通道。

    实测：176.64 13 通道 maxResults=13 → 状态 MORE，只覆盖 12/13 通道；
    maxResults=20 → OK，覆盖 13/13。
    """
    assert cap.search_max_results(13) >= 20
    assert cap.search_max_results(13) >= 13 * 2
    assert cap.search_max_results(1) >= 2
    # 有下限，避免通道数很小时 maxResults 过小
    assert cap.search_max_results(0) >= 1


def test_search_window_minutes_is_short():
    """检索窗口必须短（实测 20 分钟 + maxResults=40 触发 400）。

    且 searchResultPosition=0 返回**最早**片段，长窗口会拿到过期数据
    （探测时 192.168.10.17 用 24 小时窗口取到 09-26 上午数据，误导判断）。
    """
    assert cap.SEARCH_WINDOW_MINUTES <= 10
    assert cap.SEARCH_WINDOW_MINUTES >= 2  # 太短可能落在片段间隙


# ── 4. 移动侦测配置 ───────────────────────────────────────────────


def test_parse_motion_detection_extracts_enabled_and_sensitivity():
    """实测 11/12 台可用，enabled=true、sensitivityLevel=60。"""
    for host in ("10.18.176.10", "10.18.176.12", "10.18.176.13",
                 "10.18.176.64", "192.168.10.17"):
        md = cap.parse_motion_detection(_vmd_root(host))
        assert md is not None, host
        assert md["enabled"] is True, host
        assert md["sensitivity_level"] == 60, host


def test_parse_motion_detection_reads_nested_sensitivity():
    """sensitivityLevel 嵌在 MotionDetectionLayout 内，必须能找到。"""
    xml = """<MotionDetection version="2.0"><enabled>false</enabled>
      <MotionDetectionLayout><sensitivityLevel>35</sensitivityLevel>
      </MotionDetectionLayout></MotionDetection>"""
    md = cap.parse_motion_detection(ET.fromstring(xml))
    assert md["enabled"] is False
    assert md["sensitivity_level"] == 35


def test_parse_motion_detection_flat_sensitivity():
    """部分固件把 sensitivityLevel 平铺在根下，两种都要支持。"""
    xml = """<MotionDetection><enabled>true</enabled>
      <sensitivityLevel>80</sensitivityLevel></MotionDetection>"""
    md = cap.parse_motion_detection(ET.fromstring(xml))
    assert md["sensitivity_level"] == 80


def test_parse_motion_detection_missing_fields():
    """缺字段 → None，不得臆造默认值。"""
    md = cap.parse_motion_detection(ET.fromstring("<MotionDetection/>"))
    assert md is not None
    assert md["enabled"] is None
    assert md["sensitivity_level"] is None


def test_parse_motion_detection_none_root():
    """端点 403（176.65）→ root=None → 返回 None（不建实体）。"""
    assert cap.parse_motion_detection(None) is None


def test_parse_motion_detection_enabled_string_variants():
    """enabled 的 TRUE/True/1 都要识别为 True。"""
    for raw in ("true", "TRUE", "True", "1"):
        xml = f"<MotionDetection><enabled>{raw}</enabled></MotionDetection>"
        assert cap.parse_motion_detection(ET.fromstring(xml))["enabled"] is True
    for raw in ("false", "FALSE", "0"):
        xml = f"<MotionDetection><enabled>{raw}</enabled></MotionDetection>"
        assert cap.parse_motion_detection(ET.fromstring(xml))["enabled"] is False


# ── 5. 事件类型目录 ───────────────────────────────────────────────


def test_parse_event_types_from_triggers():
    """实测各设备事件类型数量不同，必须逐台解析（避免建幻影实体）。"""
    ets = cap.parse_event_types(_trig_root("10.18.176.12"))
    # 实测 176.12 有 18 种
    assert len(ets) == 18
    assert "VMD" in ets
    assert "fielddetection" in ets
    assert "linedetection" in ets


def test_parse_event_types_strips_channel_suffix():
    """`facedetection-1` 这类带通道后缀的 id 应归一到基础事件类型。"""
    ets = cap.parse_event_types(_trig_root("10.18.176.12"))
    assert "facedetection" in ets
    assert not any(e.endswith("-1") for e in ets), "应已剥离通道后缀"


def test_parse_event_types_differs_per_device():
    """不同设备支持的类型集合不同（能力门控的意义所在）。"""
    a = cap.parse_event_types(_trig_root("10.18.176.12"))
    b = cap.parse_event_types(_trig_root("10.18.176.13"))
    assert a != b
    assert len(b) == 12  # 实测 176.13 有 12 种


def test_parse_event_types_maps_to_known_binary_sensors():
    """事件类型 → 本集成会建的二值传感器键。

    ⚠️ 重要实测事实：`Event/triggers` 目录**不含 videoloss**（4 台设备
    全部如此），但 alertStream 实测 10/11 台推送 videoloss 事件。
    所以本测试断言的是 triggers 目录里**确实存在**的类型映射，
    而 videoloss 的映射由 test_event_types_to_sensor_keys_maps_videoloss
    单独验证（构造输入）。
    """
    ets = cap.parse_event_types(_trig_root("10.18.176.18"))
    keys = cap.event_types_to_sensor_keys(ets)
    # 176.18 实测 14 种含 VMD 与 tamperdetection
    assert "motion" in keys
    assert "tamper" in keys


def test_triggers_catalog_omits_videoloss_but_alertstream_has_it():
    """固化实测差异：triggers 目录不能作为事件传感器的门控依据。

    真机数据：
      Event/triggers  176.18/12/13/52 → 均**无** videoloss
      alertStream     10/11 台 → **有** videoloss 事件

    若用 triggers 目录门控事件传感器，video_loss 传感器将在所有设备上
    都不被创建，而设备实际正在推送该事件。这条测试是防止将来有人
    "顺手"用 triggers 目录做门控的钉子。
    """
    for host in ("10.18.176.18", "10.18.176.12", "10.18.176.13", "10.18.176.52"):
        ets = cap.parse_event_types(_trig_root(host))
        assert "videoloss" not in ets, f"{host} triggers 目录意外含 videoloss"
        assert "video_loss" not in cap.event_types_to_sensor_keys(ets)

    # 而 alertStream 实测有 videoloss（见 fixtures 的 F_alert 抓包）
    alert_files = list(FIXTURES.glob("*__F_alert__alertStream.xml"))
    assert alert_files, "alertStream fixture 缺失"
    seen_videoloss = 0
    for f in alert_files:
        txt = f.read_text(encoding="utf-8", errors="replace")
        if "<eventType>videoloss</eventType>" in txt:
            seen_videoloss += 1
    assert seen_videoloss >= 5, (
        f"实测 {seen_videoloss}/{len(alert_files)} 台 alertStream 有 videoloss，"
        f"与 triggers 目录矛盾 —— 门控必须用 alertStream 而非目录"
    )


def test_event_types_to_sensor_keys_maps_videoloss():
    """videoloss 映射本身必须可用（由 alertStream 观测到的类型驱动）。"""
    keys = cap.event_types_to_sensor_keys({"VMD", "videoloss", "tamperdetection"})
    assert keys == {"motion", "video_loss", "tamper"}


def test_event_types_to_sensor_keys_ignores_unhandled():
    """未映射的事件类型不产生传感器键（不建无意义实体）。"""
    keys = cap.event_types_to_sensor_keys({"diskfull", "ipconflict", "VMD"})
    assert keys == {"motion"}


def test_parse_event_types_none_root():
    """端点不可用（3/12 台）→ 空集合，调用方回退到 alertStream 实测类型。"""
    assert cap.parse_event_types(None) == set()


# ── 6. 录像检索可用性判定 ─────────────────────────────────────────


def test_recording_available_when_matches_present():
    """有 searchMatchItem → 可用（176.64 实测 13 通道全覆盖）。"""
    root = _root("10_18_176_64__L_track__101")
    assert cap.recording_available(root) is True


def test_recording_unavailable_on_no_matches():
    """NO MATCHES → 设备无录像存储/未配置录像，不建实体。"""
    root = _root("10_18_176_65__R_precise__exact_8ch_30min")
    assert cap.recording_available(root) is False


def test_recording_unavailable_none_root():
    """端点 403（176.51/52/53）或 401 → root=None → 不建实体。"""
    assert cap.recording_available(None) is False


def test_recording_segments_parsed_with_times():
    """解析出每 trackID 的片段起止时间（判定录像活动与最近录像时间的输入）。"""
    root = _root("10_18_176_64__L_track__101")
    segs = cap.parse_recording_segments(root)
    assert segs, "应解析出片段"
    s = segs[0]
    assert s["track_id"] == "101"
    assert s["start"].tzinfo is not None  # tz-aware，供 HA TIMESTAMP 使用
    assert s["end"].tzinfo is not None
    assert s["end"] > s["start"]


def test_recording_segments_expose_codec_and_lock():
    """附带 codecType / lockStatus（免费数据）。"""
    root = _root("10_18_176_64__L_track__101")
    segs = cap.parse_recording_segments(root)
    assert segs[0]["codec_type"] == "H.264-BP"
    assert segs[0]["lock_status"] == "unlock"


def test_recording_segments_empty_on_no_matches():
    assert cap.parse_recording_segments(None) == []
    assert cap.parse_recording_segments(
        _root("10_18_176_65__R_precise__exact_8ch_30min")) == []
