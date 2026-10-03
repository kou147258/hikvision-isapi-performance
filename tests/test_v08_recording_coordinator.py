"""v0.8 步骤5：coordinator._fetch_recording（录像检索接入 + 优雅降级）。

驱动真实方法（非正则匹配源码）。用假客户端注入 post_xml，验证：
  * 正常响应 → 返回逐通道录像状态 dict
  * 403（176.51/52/53）→ ISAPIAuthError → 返回 {}（不建录像实体）
  * 400 Invalid track id（越界 trackID）→ ISAPIError → 返回 {}
  * 连接错误 → 返回 {}（不拖垮整个刷新）
  * NO MATCHES（无录像存储）→ 返回 {}
  * trackID 按实际通道数精确生成（不越界，避免 400）
  * maxResults >= 2×通道数（避免 MORE 截断漏通道）

全部离线，假客户端不发真实网络。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import coordinator as coord_mod  # noqa: E402
from custom_components.hikvision_isapi_performance.isapi_client import (  # noqa: E402
    ISAPIAuthError,
    ISAPIConnectionError,
    ISAPIError,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures_v08"

# 真机 CMSearchResult（192.168.10.17，2 通道滚动片段）
SEG_2CH = (FIXTURES / "192_168_10_17__R_precise__exact_2ch_30min.xml").read_text(
    encoding="utf-8")
SEG_NO_MATCH = """<?xml version="1.0" encoding="UTF-8" ?>
<CMSearchResult version="2.0"><responseStatusStrg>NO MATCHES</responseStatusStrg>
<numOfMatches>0</numOfMatches></CMSearchResult>"""


class _FakeClient:
    """记录调用、可配置返回/抛异常的假客户端。"""

    def __init__(self, *, xml=None, exc=None):
        self._xml = xml
        self._exc = exc
        self.calls: list[tuple[str, str]] = []

    async def post_xml(self, path: str, body: str) -> ET.Element:
        self.calls.append((path, body))
        if self._exc is not None:
            raise self._exc
        return ET.fromstring(self._xml)


def _make_coord() -> coord_mod.HikvisionISAPICoordinator:
    """构造一个最小 coordinator（不触发 __init__ 的重逻辑）。"""
    c = coord_mod.HikvisionISAPICoordinator.__new__(
        coord_mod.HikvisionISAPICoordinator)
    c._host = "192.168.10.17"
    return c


def _channels(ids: list[str]) -> list[dict[str, Any]]:
    return [{"id": i, "name": f"ch{i}"} for i in ids]


@pytest.mark.asyncio
async def test_fetch_recording_returns_per_channel_status():
    """正常响应 → 逐通道录像状态。"""
    c = _make_coord()
    client = _FakeClient(xml=SEG_2CH)
    # now 落在片段区间内，判定为在录
    now = datetime(2026, 9, 27, 0, 56, 0, tzinfo=timezone.utc)
    result = await c._fetch_recording(client, _channels(["1", "2"]), now=now)
    assert set(result.keys()) == {"1", "2"}
    assert result["1"]["recording_active"] is True
    assert result["2"]["recording_active"] is True


@pytest.mark.asyncio
async def test_fetch_recording_sends_one_post_for_all_channels():
    """一次 POST 覆盖全部通道（批量），不逐通道发多次。"""
    c = _make_coord()
    client = _FakeClient(xml=SEG_2CH)
    await c._fetch_recording(client, _channels(["1", "2"]),
                             now=datetime(2026, 9, 27, 0, 56, tzinfo=timezone.utc))
    assert len(client.calls) == 1, "应只发一次批量检索"
    path, body = client.calls[0]
    assert path == "/ISAPI/ContentMgmt/search"
    assert "<trackID>101</trackID>" in body
    assert "<trackID>201</trackID>" in body


@pytest.mark.asyncio
async def test_fetch_recording_track_ids_match_channel_count():
    """trackID 精确匹配通道数（13 通道 NVR → 101..1301，不越界）。"""
    c = _make_coord()
    ids = [str(i) for i in range(1, 14)]
    client = _FakeClient(xml=SEG_NO_MATCH)
    await c._fetch_recording(client, _channels(ids),
                             now=datetime(2026, 9, 27, 0, 56, tzinfo=timezone.utc))
    _, body = client.calls[0]
    # 13 个 trackID 全在，且不出现第 14 个
    for i in range(1, 14):
        assert f"<trackID>{i}01</trackID>" in body
    assert "<trackID>1401</trackID>" not in body


@pytest.mark.asyncio
async def test_fetch_recording_max_results_scales():
    """maxResults >= 2×通道数，避免 MORE 截断漏通道。"""
    c = _make_coord()
    ids = [str(i) for i in range(1, 14)]
    client = _FakeClient(xml=SEG_NO_MATCH)
    await c._fetch_recording(client, _channels(ids),
                             now=datetime(2026, 9, 27, 0, 56, tzinfo=timezone.utc))
    _, body = client.calls[0]
    import re
    mr = int(re.search(r"<maxResults>(\d+)</maxResults>", body).group(1))
    assert mr >= 2 * 13, f"maxResults={mr} 不足以覆盖 13 通道"


@pytest.mark.asyncio
async def test_fetch_recording_no_match_returns_empty():
    """NO MATCHES（无录像存储）→ 空 dict（不建录像实体）。"""
    c = _make_coord()
    client = _FakeClient(xml=SEG_NO_MATCH)
    result = await c._fetch_recording(client, _channels(["1", "2"]),
                                      now=datetime(2026, 9, 27, 0, 56, tzinfo=timezone.utc))
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_recording_403_returns_empty():
    """403（176.51/52/53）→ 空 dict，不抛异常拖垮刷新。"""
    c = _make_coord()
    client = _FakeClient(exc=ISAPIAuthError("403", status_code=403))
    result = await c._fetch_recording(client, _channels(["1"]),
                                      now=datetime(2026, 9, 27, 0, 56, tzinfo=timezone.utc))
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_recording_400_invalid_track_returns_empty():
    """400 Invalid track id → 空 dict（降级，不崩）。"""
    c = _make_coord()
    client = _FakeClient(exc=ISAPIError("400 Invalid track id", status_code=400))
    result = await c._fetch_recording(client, _channels(["1", "2", "3"]),
                                      now=datetime(2026, 9, 27, 0, 56, tzinfo=timezone.utc))
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_recording_connection_error_returns_empty():
    """连接错误 → 空 dict。"""
    c = _make_coord()
    client = _FakeClient(exc=ISAPIConnectionError("timeout"))
    result = await c._fetch_recording(client, _channels(["1"]),
                                      now=datetime(2026, 9, 27, 0, 56, tzinfo=timezone.utc))
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_recording_no_channels_skips_request():
    """无通道 → 不发请求，返回空（IPC 无录像存储也走这条）。"""
    c = _make_coord()
    client = _FakeClient(xml=SEG_2CH)
    result = await c._fetch_recording(client, [],
                                      now=datetime(2026, 9, 27, 0, 56, tzinfo=timezone.utc))
    assert result == {}
    assert client.calls == [], "无通道不应发检索请求"


@pytest.mark.asyncio
async def test_fetch_recording_non_numeric_channel_ids_skipped():
    """非数字通道 id（异常设备）跳过，不生成无效 trackID。"""
    c = _make_coord()
    client = _FakeClient(xml=SEG_NO_MATCH)
    await c._fetch_recording(client, _channels(["abc", "1"]),
                             now=datetime(2026, 9, 27, 0, 56, tzinfo=timezone.utc))
    # 至少不因非数字 id 崩溃；有效通道 1 仍在
    _, body = client.calls[0]
    assert "<trackID>101</trackID>" in body


@pytest.mark.asyncio
async def test_fetch_recording_defaults_now_to_real_clock():
    """不传 now 时用真实时钟（生产路径），不抛异常。"""
    c = _make_coord()
    client = _FakeClient(xml=SEG_2CH)
    result = await c._fetch_recording(client, _channels(["1", "2"]))
    # fixture 是旧数据，now 远在其后 → 判定为未在录（但仍有 last_recording_time）
    assert set(result.keys()) == {"1", "2"}
    assert result["1"]["recording_active"] is False
