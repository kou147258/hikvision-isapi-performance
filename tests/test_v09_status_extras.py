"""v0.9 批次1：/System/status 里被丢弃的「零网络成本」字段 + reboot_count 门控修复。

背景（用户诉求）：
  "ha 里边还有部分实体没有。有部分也还是显示未知，你把能探测到的全套加进去"

真机探测事实（12 台，probe_captures/v09_wide，2026-10-04 抓取）：
  ``/ISAPI/System/status`` 12/12 可用，集成【早已在抓】，但解析器只取了
  8 个字段，把同一响应里的这些数据全部丢弃：

    DomeInfoList  球机寿命：6/12（仅球机 IPC：176.10/12/13/51/52/53）
    CameraList    镜头动作计数：6/12（同上）
    memoryDescription：12/12（"DDR Memory"）
    cpuDescription：11/12（R17.17 无 CPUList）
    batteryAllowance：1/12（仅 176.10）
    videoRewritingTimes：1/12（仅 176.10，SD 卡重写次数）

  这些是【零新增请求】的数据 —— 只需解析已抓到的响应，故为批次1。

单位自校验（真机数据证明，非推断）：
  176.10: runTimeUnderNegativetwenty(0) + runTimeBetweenNtwentyPforty(248171)
          + runtimeOverPositiveforty(360134) = 608305 = domeRunTotalTime
  6 台球机全部满足「三温度区间之和 == domeRunTotalTime」，
  且量级与 deviceUpTime 一致 → 确认单位为【秒】。

同时修复 v0.8 遗留缺陷（reboot_count 显示未知）：
  coordinator._parse_system_status 无条件写入 "rebootCount" 键（值可为 None），
  使 sensor.py 的存在性门控 `"rebootCount" not in status` 永远为 False，
  6 台不上报该字段的设备被建出永远「未知」的实体。
  实测：176.16/17/18 + 176.64/65 + R17.17 均无 <totalRebootCount>。
  capabilities.has_reboot_count(root) 的实现本来就是对的，
  问题出在解析器与门控的契约不一致（解析器 docstring 声称按存在性门控，
  实际却总是写键）。

命名空间说明：
  fixture 保留真机原始响应（带 xmlns），与生产一致地在加载时剥离 ——
  isapi_client.get_xml() 同样先 _strip_xmlns 再解析，
  故 capabilities 的裸标签 find() 在生产与测试中行为一致。
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
from custom_components.hikvision_isapi_performance import coordinator as coord  # noqa: E402
from custom_components.hikvision_isapi_performance.isapi_client import (  # noqa: E402
    _strip_xmlns,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures_v09"

# 真机有球机/镜头块的 6 台 IPC
DOME_HOSTS = ["176.10", "176.12", "176.13", "176.51", "176.52", "176.53"]
# 真机无球机/镜头块的设备（3 台定焦 IPC + 3 台 NVR）
NO_DOME_HOSTS = ["176.16", "176.17", "176.18", "176.64", "176.65", "R17.17"]
ALL_HOSTS = DOME_HOSTS + NO_DOME_HOSTS
IP2KEY = {
    "176.10": "10_18_176_10", "176.12": "10_18_176_12", "176.13": "10_18_176_13",
    "176.16": "10_18_176_16", "176.17": "10_18_176_17", "176.18": "10_18_176_18",
    "176.51": "10_18_176_51", "176.52": "10_18_176_52", "176.53": "10_18_176_53",
    "176.64": "10_18_176_64", "176.65": "10_18_176_65", "R17.17": "192_168_10_17",
}


def _root(host: str) -> ET.Element:
    """加载真机 status fixture，剥离命名空间（与生产 get_xml 一致）。"""
    files = sorted(FIXTURES.glob(f"{IP2KEY[host]}__G_status__System_status.xml"))
    assert files, f"missing v09 status fixture for {host}"
    return ET.fromstring(_strip_xmlns(files[0].read_text(encoding="utf-8", errors="replace")))


# ══════════════════════════════════════════════════════════════════
# 1. 球机寿命 parse_dome_info
# ══════════════════════════════════════════════════════════════════


def test_dome_info_parsed_with_exact_real_values():
    """176.10 球机寿命必须解析出真机精确值（不可用猜测值）。"""
    d = cap.parse_dome_info(_root("176.10"))
    assert d is not None
    assert d["dome_run_total_time"] == 608305
    assert d["pan_total_rounds"] == 62
    assert d["tilt_total_rounds"] == 68
    assert d["heat_state"] == 0
    assert d["fan_state"] == 1
    assert d["run_time_under_neg20"] == 0
    assert d["run_time_between_neg20_pos40"] == 248171
    assert d["run_time_over_pos40"] == 360134
    assert d["pan_freq_record"] == 996
    assert d["tilt_freq_record"] == 14


def test_dome_info_second_device_distinct_values():
    """176.12 是另一台球机，值必须与 176.10 不同（证明不是硬编码）。"""
    d = cap.parse_dome_info(_root("176.12"))
    assert d is not None
    assert d["dome_run_total_time"] == 15110
    assert d["pan_total_rounds"] == 0
    assert d["tilt_total_rounds"] == 1
    assert d["run_time_between_neg20_pos40"] == 7
    assert d["run_time_over_pos40"] == 15103


def test_dome_info_zero_values_are_real_not_missing():
    """176.13/51/52/53 球机字段全为 0 —— 0 是有效读数，必须返回 0 而非 None。

    这是三态语义：字段存在且为 0 ≠ 字段不存在。若把 0 当缺失处理，
    这些设备会显示「未知」，正是用户抱怨的症状。
    """
    for host in ("176.13", "176.51", "176.52", "176.53"):
        d = cap.parse_dome_info(_root(host))
        assert d is not None, f"{host} 应有 DomeInfoList"
        assert d["dome_run_total_time"] == 0, host
        assert d["pan_total_rounds"] == 0, host
        assert d["tilt_total_rounds"] == 0, host
        assert d["heat_state"] == 0, host
        assert d["fan_state"] == 0, host


def test_dome_info_none_on_non_dome_devices():
    """定焦 IPC 与 NVR 无 DomeInfoList → 返回 None（不建实体）。"""
    for host in NO_DOME_HOSTS:
        assert cap.parse_dome_info(_root(host)) is None, host


def test_dome_info_none_for_failed_endpoint():
    """端点失败（root=None）→ None，不得抛异常。"""
    assert cap.parse_dome_info(None) is None


def test_dome_info_temperature_ranges_sum_to_total():
    """单位=秒的自校验：三温度区间之和必须等于 domeRunTotalTime。

    真机数据证明（6 台球机全部成立）。若解析错字段或单位，此断言会失败。
    """
    for host in DOME_HOSTS:
        d = cap.parse_dome_info(_root(host))
        assert d is not None, host
        total = (
            d["run_time_under_neg20"]
            + d["run_time_between_neg20_pos40"]
            + d["run_time_over_pos40"]
        )
        assert total == d["dome_run_total_time"], f"{host}: 区间和{total} != 总时长{d['dome_run_total_time']}"


# ══════════════════════════════════════════════════════════════════
# 2. 镜头动作计数 parse_camera_usage
# ══════════════════════════════════════════════════════════════════


def test_camera_usage_parsed_with_exact_real_values():
    """176.10 镜头动作计数必须是真机精确值。"""
    c = cap.parse_camera_usage(_root("176.10"))
    assert c is not None
    assert c["camera_run_total_time"] == 608305
    assert c["zoom_total_steps"] == 890
    assert c["focus_total_steps"] == 121
    assert c["iris_total_steps"] == 1037
    assert c["icr_total_steps"] == 194
    assert c["zoom_reverse_times"] == 590
    assert c["focus_reverse_times"] == 9417
    assert c["iris_shift_times"] == 172488
    assert c["icr_shift_times"] == 719
    assert c["lens_init_times"] == 0


def test_camera_usage_zero_values_are_real():
    """176.12/13/51/52/53 镜头计数为 0（全新未动作）→ 必须返回 0 而非 None。"""
    for host in ("176.12", "176.13", "176.51", "176.52", "176.53"):
        c = cap.parse_camera_usage(_root(host))
        assert c is not None, host
        assert c["zoom_total_steps"] == 0, host
        assert c["focus_total_steps"] == 0, host


def test_camera_usage_matches_dome_runtime_on_dome_devices():
    """球机上 cameraRunTotalTime == domeRunTotalTime（同一机械运行时长）。

    真机 6 台全部成立，可作为交叉校验。
    """
    for host in DOME_HOSTS:
        c = cap.parse_camera_usage(_root(host))
        d = cap.parse_dome_info(_root(host))
        assert c is not None and d is not None, host
        assert c["camera_run_total_time"] == d["dome_run_total_time"], host


def test_camera_usage_none_on_devices_without_camaralist():
    """无 CameraList 的设备 → None。"""
    for host in NO_DOME_HOSTS:
        assert cap.parse_camera_usage(_root(host)) is None, host


def test_camera_usage_none_for_failed_endpoint():
    assert cap.parse_camera_usage(None) is None


# ══════════════════════════════════════════════════════════════════
# 3. 其它被丢弃字段 parse_status_extras
# ══════════════════════════════════════════════════════════════════


def test_status_extras_memory_description_on_all_devices():
    """memoryDescription 12/12 全设备可用，值必须是真机字符串。"""
    for host in ALL_HOSTS:
        x = cap.parse_status_extras(_root(host))
        assert x.get("memory_description") == "DDR Memory", host


def test_status_extras_cpu_description_present_except_one_nvr():
    """cpuDescription 11/12：R17.17 无 CPUList → None；其余有真机字符串。"""
    for host in ALL_HOSTS:
        x = cap.parse_status_extras(_root(host))
        if host == "R17.17":
            assert x.get("cpu_description") is None, host
        else:
            assert x.get("cpu_description"), f"{host} 应有 cpuDescription"
    # 176.10 的精确值（真机抓包）
    x = cap.parse_status_extras(_root("176.10"))
    assert x["cpu_description"] == "ARM926EJ-Sid(wb) [41069265] revision 5 (ARMv5TEJ)"


def test_status_extras_battery_and_sd_rewrite_only_on_one_device():
    """batteryAllowance / videoRewritingTimes 仅 176.10 有。

    176.10: batteryAllowance=0（0 是有效值！不是缺失）, videoRewritingTimes=3706
    其余 11 台：两者都应为 None（字段不存在）。
    """
    x = cap.parse_status_extras(_root("176.10"))
    assert x["battery_allowance"] == 0
    assert x["video_rewriting_times"] == 3706

    for host in ALL_HOSTS:
        if host == "176.10":
            continue
        e = cap.parse_status_extras(_root(host))
        assert e.get("battery_allowance") is None, f"{host} 不该有 batteryAllowance"
        assert e.get("video_rewriting_times") is None, f"{host} 不该有 videoRewritingTimes"


def test_status_extras_never_raises_on_none():
    """端点失败 → 返回空 dict（各键缺失），不抛异常。"""
    x = cap.parse_status_extras(None)
    assert isinstance(x, dict)
    assert x.get("memory_description") is None
    assert x.get("cpu_description") is None


# ══════════════════════════════════════════════════════════════════
# 4. v0.8 遗留缺陷：reboot_count 门控失效（显示未知的真因）
# ══════════════════════════════════════════════════════════════════


def test_reboot_count_key_absent_when_device_does_not_report():
    """★缺陷修复：设备不上报 totalRebootCount 时，解析结果【不得含该键】。

    修复前：coordinator._parse_system_status 无条件写 "rebootCount": None，
    使 sensor.py 的门控 `"rebootCount" not in status` 永远 False，
    6 台设备被建出永远显示「未知」的实体。
    """
    for host in NO_DOME_HOSTS:  # 这 6 台正好也是无 totalRebootCount 的 6 台
        st = coord._parse_system_status(_root(host))
        assert "rebootCount" not in st, f"{host} 不上报该字段，键不应存在"


def test_reboot_count_key_present_with_value_when_reported():
    """上报该字段的 6 台：键必须存在且为真机值（0 也算有效）。"""
    expected = {"176.10": "41", "176.12": "1", "176.13": "0",
                "176.51": "0", "176.52": "0", "176.53": "0"}
    for host, want in expected.items():
        st = coord._parse_system_status(_root(host))
        assert "rebootCount" in st, host
        assert st["rebootCount"] == want, f"{host}: got {st['rebootCount']!r} want {want!r}"


def test_reboot_count_gate_now_blocks_non_reporting_devices():
    """端到端验证门控：复现 sensor.py:963 的判定，6 台必须被拦截。"""
    blocked, allowed = [], []
    for host in ALL_HOSTS:
        st = coord._parse_system_status(_root(host))
        # sensor.py _make_reboot_count_listener 的门控条件
        if "rebootCount" not in st:
            blocked.append(host)
        else:
            allowed.append(host)
    assert sorted(blocked) == sorted(NO_DOME_HOSTS), f"应拦截6台，实际 {blocked}"
    assert sorted(allowed) == sorted(DOME_HOSTS), f"应放行6台，实际 {allowed}"


def test_reboot_count_gate_agrees_with_capabilities_helper():
    """解析器门控必须与 capabilities.has_reboot_count 语义一致（两个真相源不能矛盾）。"""
    for host in ALL_HOSTS:
        root = _root(host)
        st = coord._parse_system_status(root)
        assert ("rebootCount" in st) == cap.has_reboot_count(root), host


def test_reboot_count_none_root_returns_empty_status():
    """端点失败时解析结果不得含 rebootCount 键（否则又建出未知实体）。"""
    st = coord._parse_system_status(None)
    assert "rebootCount" not in st


# ══════════════════════════════════════════════════════════════════
# 5. 解析结果整合：system_status 必须携带新字段供 sensor 消费
# ══════════════════════════════════════════════════════════════════


def test_parse_system_status_carries_dome_and_camera():
    """176.10 的 system_status 必须带 dome_info / camera_usage 子字典。"""
    st = coord._parse_system_status(_root("176.10"))
    assert st.get("dome_info"), "应含 dome_info"
    assert st["dome_info"]["pan_total_rounds"] == 62
    assert st.get("camera_usage"), "应含 camera_usage"
    assert st["camera_usage"]["zoom_total_steps"] == 890


def test_parse_system_status_carries_extras():
    """system_status 必须带 memoryDescription（12/12 全设备）。

    数值型 extras 存为 int（与 parse_status_extras 一致），
    不再走 system_status 旧有的「全部转字符串」惯例 —— 因为
    dome_info/camera_usage 本身就是 int 字典，混用两种表示会让
    sensor 层的 _safe_int 处理两套语义。
    """
    st = coord._parse_system_status(_root("176.10"))
    assert st.get("memoryDescription") == "DDR Memory"
    assert st.get("videoRewritingTimes") == 3706
    assert st.get("batteryAllowance") == 0


def test_parse_system_status_omits_dome_when_absent():
    """无球机块的设备：dome_info / camera_usage 不得出现（避免建空实体）。"""
    for host in NO_DOME_HOSTS:
        st = coord._parse_system_status(_root(host))
        assert not st.get("dome_info"), host
        assert not st.get("camera_usage"), host


def test_parse_system_status_existing_fields_unaffected():
    """回归保护：v0.8 已有字段必须原样保留，不受批次1改动影响。

    ⚠ memoryAvailable 的期望值是【归一化后】的 MB，不是真机原始值。
    176.10 真机 memoryUsage=98(MB)、memoryAvailable=13632(KB)，
    v0.6.25 启发式判定 13632 > 98*50 → 认定 available 是 KB，
    换算为 13632/1024 = 13 MB。交叉验证：98 + 13 ≈ 111 MB 总内存、
    占用 88%，与 memoryUsage=98% 量级自洽，故 13 是正确值。
    """
    st = coord._parse_system_status(_root("176.10"))
    assert st["cpuUtilization"] == "16"
    assert st["memoryUsage"] == "98"
    assert st["memoryAvailable"] == "13"
    assert st["uptime"] == "762399"
    assert st["deviceStatus"] == "Unknown"  # 真机响应无 deviceStatus 字段
    assert st["currentDeviceTime"] == "2026-10-04T04:01:27+08:00"
    assert st["cpuDescription"] == "ARM926EJ-Sid(wb) [41069265] revision 5 (ARMv5TEJ)"


def test_parse_system_status_nvr_memory_mb_normalisation_unchanged():
    """回归保护：NVR 小数 MB 归一化行为不变（v0.6.25 启发式）。

    R17.17 真机：memoryUsage=399.062500 MB, memoryAvailable=551.023438 MB
    两者同量级 → 不触发 KB→MB 换算（551 < 399*50），应各自取整。
    """
    st = coord._parse_system_status(_root("R17.17"))
    assert st["memoryUsage"] == "399"
    assert st["memoryAvailable"] == "551"
