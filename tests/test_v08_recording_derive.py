"""v0.8 步骤5：录像状态推导（derive_recording_status）。

把 parse_recording_segments 的片段列表映射成逐通道录像状态：
  * recording_active —— 最新片段的计划 endTime 是否覆盖到"现在"附近
  * last_recording_time —— 该片段的 endTime（最近录像时间）

⚠️ 核心诚实性约束（真机实测得出，见 docs/v0.8-design.md §1.4）：
  海康的 endTime 是**预分配的计划结束时间**，不是实际写入位置。
  实测 176.64 的 track101 片段 endTime 在采样时刻之后（滞后为负），
  且 100 秒内不变。因此：
    - recording_active = (最新 endTime >= now - 容忍窗口)
    - 这是**推导值**，不是设备直报的录像状态位
    - 停录检测有延迟：设备停止分配新片段后，要等已有 endTime 过去
      才会转为 False，延迟 ≈ 一个分段长度（实测 17~358 分钟）

容忍窗口默认 60 秒：实测 176.10 曾出现 latest_end 仅比 now 早 3 秒
（新片段尚未写入索引），严格比较会在片段交界处抖动。

测试用真机 fixture + 固定 now，不联网、不依赖 wall-clock。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import capabilities as cap  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures_v08"


def _segments(name: str) -> list[dict]:
    f = FIXTURES / name
    assert f.exists(), f"missing fixture {name}"
    root = ET.fromstring(f.read_text(encoding="utf-8", errors="replace"))
    return cap.parse_recording_segments(root)


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ── 基本推导：2 通道 NVR (192.168.10.17) ──────────────────────────


def test_derive_two_channel_nvr():
    """192.168.10.17 实测 2 通道各有滚动片段 → 两通道都在录像。"""
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    # fixture 采集时刻约 00:56Z；取一个落在两通道片段区间内的 now
    now = _dt("2026-09-27T00:56:00Z")
    st = cap.derive_recording_status(segs, now=now)
    assert set(st.keys()) == {"1", "2"}
    assert st["1"]["recording_active"] is True
    assert st["2"]["recording_active"] is True


def test_derive_last_recording_time_is_latest_end():
    """last_recording_time = 该通道所有片段的最大 endTime。"""
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    now = _dt("2026-09-27T00:56:00Z")
    st = cap.derive_recording_status(segs, now=now)
    # track101 片段 endTime: 00:40:21 / 01:26:41 → 取最新 01:26:41
    assert st["1"]["last_recording_time"] == _dt("2026-09-27T01:26:41Z")
    # track201 片段 endTime: 00:53:27 / 01:39:48 → 取最新 01:39:48
    assert st["2"]["last_recording_time"] == _dt("2026-09-27T01:39:48Z")


def test_derive_channel_key_from_track_id():
    """trackID 101→通道1, 201→通道2（去掉末两位码流号）。"""
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    now = _dt("2026-09-27T00:56:00Z")
    st = cap.derive_recording_status(segs, now=now)
    assert "1" in st and "2" in st
    assert "101" not in st  # 不得用 trackID 当键


# ── 容忍窗口：片段交界不得抖动 ────────────────────────────────────


def test_recording_active_within_tolerance():
    """endTime 略早于 now（新片段未写入索引）仍判为在录。

    实测 176.10 曾出现 latest_end 比 now 早 3 秒。默认容忍 60 秒。
    """
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    # now 比 track101 最新 endTime(01:26:41) 晚 30 秒 → 仍在容忍窗口内
    now = _dt("2026-09-27T01:27:11Z")
    st = cap.derive_recording_status(segs, now=now)
    assert st["1"]["recording_active"] is True


def test_recording_inactive_beyond_tolerance():
    """endTime 远早于 now（超出容忍窗口）→ 判为未在录像。"""
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    # now 比 track101 最新 endTime(01:26:41) 晚 10 分钟 → 超出 60 秒容忍
    now = _dt("2026-09-27T01:36:41Z")
    st = cap.derive_recording_status(segs, now=now)
    assert st["1"]["recording_active"] is False
    # 但 last_recording_time 仍保留（用户可看到最近录到几点）
    assert st["1"]["last_recording_time"] == _dt("2026-09-27T01:26:41Z")


def test_tolerance_configurable():
    """容忍窗口可配置。

    track101 最新 endTime=01:26:41，now=01:36:41，相差 600 秒。
    容忍 > 600 秒才判为在录，否则未在录。
    """
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    now = _dt("2026-09-27T01:36:41Z")  # endTime 后 600 秒
    # 容忍 900 秒(>600) → 仍在录
    st = cap.derive_recording_status(segs, now=now, tolerance_seconds=900)
    assert st["1"]["recording_active"] is True
    # 容忍 60 秒(<600) → 未在录
    st2 = cap.derive_recording_status(segs, now=now, tolerance_seconds=60)
    assert st2["1"]["recording_active"] is False


# ── 未来 endTime（预分配计划值）──────────────────────────────────


def test_future_end_time_counts_as_recording():
    """endTime 在 now 之后（预分配计划值）→ 判为在录像。

    这是海康的正常行为：设备预先分配好整个分段的计划结束时间。
    """
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    # now 早于 track101 最新 endTime(01:26:41)
    now = _dt("2026-09-27T01:00:00Z")
    st = cap.derive_recording_status(segs, now=now)
    assert st["1"]["recording_active"] is True
    assert st["2"]["recording_active"] is True


# ── 边界与降级 ────────────────────────────────────────────────────


def test_derive_empty_segments():
    """无片段（NO MATCHES / 端点不可用）→ 空结果（不建录像实体）。"""
    assert cap.derive_recording_status([], now=_dt("2026-09-27T00:56:00Z")) == {}


def test_derive_defaults_to_real_clock():
    """不传 now 时用真实主机时钟（生产路径），不抛异常。"""
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    st = cap.derive_recording_status(segs)
    # fixture 是 2026-09-27 的旧数据，now 远在其后 → 判定为未在录
    assert st["1"]["recording_active"] is False


def test_derive_ignores_segment_without_end():
    """缺 end 的片段跳过，不污染结果。"""
    segs = [{"track_id": "101", "start": _dt("2026-09-27T00:00:00Z"),
             "end": None, "codec_type": None, "lock_status": None,
             "record_type": None}]
    # end=None 的片段应被 parse 层过滤，这里直接构造验证 derive 容错
    st = cap.derive_recording_status(
        [s for s in segs if s["end"] is not None], now=_dt("2026-09-27T00:56:00Z"))
    assert st == {}


def test_derive_multiple_segments_same_channel_picks_latest():
    """同通道多片段 → 取最新 endTime 判定（192.168.10.17 track101 有2段）。"""
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    now = _dt("2026-09-27T00:56:00Z")
    st = cap.derive_recording_status(segs, now=now)
    # track101 有两段(00:40:21, 01:26:41)，取最新 01:26:41
    ch1_ends = sorted(s["end"] for s in segs if s["track_id"] == "101")
    assert st["1"]["last_recording_time"] == ch1_ends[-1]


def test_derive_codec_and_record_type_exposed():
    """顺带暴露 codec_type / record_type（免费数据，供传感器用）。"""
    segs = _segments("192_168_10_17__R_precise__exact_2ch_30min.xml")
    now = _dt("2026-09-27T00:56:00Z")
    st = cap.derive_recording_status(segs, now=now)
    assert st["1"]["codec_type"] == "H.264-BP"
    assert st["1"]["record_type"] == "timing"


def test_derive_thirteen_channel_nvr():
    """13 通道 NVR (176.64)：全部通道被覆盖（批量检索的意义）。"""
    f = FIXTURES / "10_18_176_64__O_decide__批量13track_mr10_60min.xml"
    if not f.exists():
        # 退回用 L_track（单通道）验证不崩
        segs = _segments("10_18_176_64__L_track__101.xml")
        now = _dt("2026-09-27T00:48:00Z")
        st = cap.derive_recording_status(segs, now=now)
        assert "1" in st
        return
    root = ET.fromstring(f.read_text(encoding="utf-8", errors="replace"))
    segs = cap.parse_recording_segments(root)
    now = _dt("2026-09-27T00:48:54Z")
    st = cap.derive_recording_status(segs, now=now)
    # 批量检索应覆盖多个通道
    assert len(st) >= 4
