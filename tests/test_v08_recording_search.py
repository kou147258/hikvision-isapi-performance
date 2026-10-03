"""v0.8 步骤5：录像检索请求体生成（build_search_body）。

POST /ISAPI/ContentMgmt/search 的请求体格式已在真机验证（12 台探测，
返回 200 的参数组合见 probe_decisive_recording.py / probe_search_bounds.py）。
本测试锁定该格式，防止将来改坏。

关键约束（真机实测）：
  * trackID 必须精确（越界 trackID 会让部分设备整个请求 400）
  * maxResults >= 2×通道数（否则 MORE 截断漏通道）
  * 窗口短（searchResultPosition=0 返回最早片段，长窗口拿到过期数据）
  * searchID 唯一（设备按它去重/分页）

build_search_body 是纯函数：now 与 search_id 可注入，便于测试。
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import capabilities as cap  # noqa: E402

NOW = datetime(2026, 9, 27, 0, 56, 0, tzinfo=timezone.utc)
FIXED_ID = "TEST-SEARCH-ID"


def test_body_contains_all_track_ids():
    """每个 trackID 都出现在 trackIDList 里。"""
    body = cap.build_search_body(["101", "201", "301"], 5, 20,
                                 now=NOW, search_id=FIXED_ID)
    for t in ("101", "201", "301"):
        assert f"<trackID>{t}</trackID>" in body


def test_body_single_track():
    body = cap.build_search_body(["101"], 5, 20, now=NOW, search_id=FIXED_ID)
    assert body.count("<trackID>") == 1
    assert "<trackID>101</trackID>" in body


def test_body_time_window():
    """startTime = now - minutes_back，endTime = now，UTC Z 格式。"""
    body = cap.build_search_body(["101"], 5, 20, now=NOW, search_id=FIXED_ID)
    # now=00:56:00, 5 分钟前=00:51:00
    assert "<startTime>2026-09-27T00:51:00Z</startTime>" in body
    assert "<endTime>2026-09-27T00:56:00Z</endTime>" in body


def test_body_max_results_and_position():
    body = cap.build_search_body(["101"], 5, 26, now=NOW, search_id=FIXED_ID)
    assert "<maxResults>26</maxResults>" in body
    # position=0：从头取（配合短窗口，取到的就是最近片段）
    assert "<searchResultPosition>0</searchResultPosition>" in body


def test_body_search_id_used():
    """注入的 search_id 出现在 body（便于测试；生产用 uuid）。"""
    body = cap.build_search_body(["101"], 5, 20, now=NOW, search_id=FIXED_ID)
    assert f"<searchID>{FIXED_ID}</searchID>" in body


def test_body_default_search_id_is_unique():
    """不传 search_id 时每次生成唯一值（设备按它去重）。"""
    b1 = cap.build_search_body(["101"], 5, 20, now=NOW)
    b2 = cap.build_search_body(["101"], 5, 20, now=NOW)
    id1 = b1.split("<searchID>")[1].split("</searchID>")[0]
    id2 = b2.split("<searchID>")[1].split("</searchID>")[0]
    assert id1 != id2
    # 两者都是合法 UUID
    uuid.UUID(id1.strip())
    uuid.UUID(id2.strip())


def test_body_has_metadata_descriptor():
    """必须带 recordType metadataDescriptor，否则设备不按录像类型过滤。"""
    body = cap.build_search_body(["101"], 5, 20, now=NOW, search_id=FIXED_ID)
    assert "recordType.meta" in body
    assert "<metadataDescriptor>" in body


def test_body_root_element():
    body = cap.build_search_body(["101"], 5, 20, now=NOW, search_id=FIXED_ID)
    assert "<CMSearchDescription>" in body
    assert body.strip().startswith("<?xml")


def test_body_naive_now_treated_as_utc():
    """传 naive datetime（无时区）时按 UTC 处理，不抛异常。"""
    naive = datetime(2026, 9, 27, 0, 56, 0)  # 无 tzinfo
    body = cap.build_search_body(["101"], 5, 20, now=naive, search_id=FIXED_ID)
    assert "<endTime>2026-09-27T00:56:00Z</endTime>" in body


def test_body_defaults_to_real_clock():
    """不传 now 时用真实时钟（生产路径），endTime 接近当前 UTC。"""
    body = cap.build_search_body(["101"], 5, 20, search_id=FIXED_ID)
    end = body.split("<endTime>")[1].split("</endTime>")[0]
    end_dt = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    delta = abs((datetime.now(timezone.utc) - end_dt).total_seconds())
    assert delta < 5, f"endTime 应接近当前时刻，偏差 {delta}s"


def test_body_matches_device_accepted_format():
    """整体验证：body 结构与真机接受（200）的格式一致。

    真机验证过的格式要素：CMSearchDescription 根、trackIDList、
    timeSpanList/timeSpan、maxResults、searchResultPosition、metadataList。
    """
    body = cap.build_search_body(["101", "201"], 5, 20,
                                 now=NOW, search_id=FIXED_ID)
    for required in (
        "<CMSearchDescription>",
        "<trackIDList>", "<trackID>101</trackID>", "<trackID>201</trackID>",
        "<timeSpanList>", "<timeSpan>", "<startTime>", "<endTime>",
        "<maxResults>20</maxResults>", "<searchResultPosition>0</searchResultPosition>",
        "<metadataList>", "recordType.meta",
    ):
        assert required in body, f"缺 {required}"
