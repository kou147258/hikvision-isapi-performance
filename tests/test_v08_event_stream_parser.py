"""v0.8 步骤8：alertStream multipart 解析器（event_stream.py）。

真机实测（fixtures_v08 的 F_alert 抓包，11 台设备）：
  * 12/12 台可用（176.18 需 Basic 认证）
  * multipart/mixed，`--boundary` 分帧，每帧 Content-Type + Content-Length + XML
  * 事件负载含 channelID / dynChannelID / eventType / eventState(active|inactive)
    / channelName / dateTime / activePostCount
  * NVR 176.65 与 192.168.10.17 的 channelID=0（设备级事件，非通道级）
  * **消防水管风险**：176.10 在 8 秒产出 2,252,800 字节（≈280 KB/s），
    是次高设备（176.64，28,672 字节）的 78 倍。无界缓冲会吃光内存。

因此解析器必须是：
  1. **增量式**：feed(chunk) 可跨任意分割边界，半帧留待下次
  2. **有界缓冲**：超过上限丢弃最旧字节，绝不无限增长
  3. **去抖**：相同 (channelID, eventType, eventState) 不重复派发
  4. **容错**：坏帧跳过而不崩溃（流式数据里 XML 可能被截断）

纯解析，无网络。测试用真机抓包。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import event_stream as es  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures_v08"


def _payload(name: str) -> bytes:
    """读真机抓包，去掉探测脚本加的 `[NB]` 头部标记。"""
    f = FIXTURES / name
    assert f.exists(), f"missing fixture {name}"
    raw = f.read_text(encoding="utf-8", errors="replace")
    lines = raw.split("\n")
    if lines and lines[0].startswith("[") and lines[0].endswith("]"):
        lines = lines[1:]
    return "\n".join(lines).encode("utf-8")


NVR_64 = "10_18_176_64__F_alert__alertStream.xml"
FLOOD_10 = "10_18_176_10__F_alert__alertStream.xml"
DEVICE_LEVEL_65 = "10_18_176_65__F_alert__alertStream.xml"
VMD_AND_VL_12 = "10_18_176_12__F_alert__alertStream.xml"


def _feed_all(parser, payload: bytes, chunk: int = 4096) -> list:
    """按 chunk 分块喂入，收集全部事件（模拟流式读取）。"""
    out = []
    for i in range(0, len(payload), chunk):
        out.extend(parser.feed(payload[i:i + chunk]))
    return out


# ── 1. 真机负载解析 ────────────────────────────────────────────────


def test_parses_real_nvr_payload():
    """176.64 实测 8 秒 46 个 VMD 事件、6 个通道 → 全部解析出来。"""
    p = es.MultipartEventParser()
    events = _feed_all(p, _payload(NVR_64))
    assert len(events) == 46
    assert all(e.event_type == "VMD" for e in events)
    assert all(e.event_state == "active" for e in events)


def test_parses_channel_id_and_name():
    """事件必须带通道号与通道名（实体命名与去重都依赖它）。"""
    p = es.MultipartEventParser()
    events = _feed_all(p, _payload(NVR_64))
    first = events[0]
    assert first.channel_id == "1"
    assert first.channel_name == "摄像机12"
    assert first.event_type == "VMD"
    assert first.event_state == "active"
    # 第二个事件是另一个通道
    assert events[1].channel_id == "2"
    assert events[1].channel_name == "摄像机13"


def test_parses_mixed_event_types():
    """176.12 实测同时含 VMD 与 videoloss。

    实测组合（唯一键）：('1','VMD','active') x10、
    ('1','videoloss','inactive') x2。**没有** videoloss 的 active 样本。
    """
    p = es.MultipartEventParser()
    events = _feed_all(p, _payload(VMD_AND_VL_12))
    assert len(events) == 12
    assert {e.event_type for e in events} == {"VMD", "videoloss"}
    by_type = {}
    for e in events:
        by_type.setdefault(e.event_type, set()).add(e.event_state)
    assert by_type["VMD"] == {"active"}
    assert by_type["videoloss"] == {"inactive"}


def test_parses_device_level_events_channel_zero():
    """176.65 的 channelID=0 是设备级事件，必须能被解析（不当作通道）。"""
    p = es.MultipartEventParser()
    events = _feed_all(p, _payload(DEVICE_LEVEL_65))
    assert len(events) == 7  # 8 个开标签，末帧被截断 → 7 个完整帧
    assert all(e.channel_id == "0" for e in events)
    assert all(e.event_type == "videoloss" for e in events)
    assert all(e.event_state == "inactive" for e in events)


def test_parses_active_post_count_and_datetime():
    """附带 activePostCount 与 dateTime（供事件总线派发）。"""
    p = es.MultipartEventParser()
    events = _feed_all(p, _payload(NVR_64))
    e = events[0]
    assert e.active_post_count is not None
    assert e.date_time is not None
    # dateTime 来自设备：2026-09-27T08:25:13+08:00
    assert "2026-09-27" in str(e.date_time)


def test_parses_dyn_channel_id():
    """dynChannelID 与 channelID 都要保留（NVR 动态通道映射）。"""
    p = es.MultipartEventParser()
    events = _feed_all(p, _payload(NVR_64))
    assert events[0].dyn_channel_id == "1"


# ── 2. 增量解析：跨 chunk 分割 ─────────────────────────────────────


def test_split_at_arbitrary_chunk_boundaries():
    """任意分割大小都必须得到相同结果（真机流式读取必然如此）。"""
    payload = _payload(NVR_64)
    baseline = _feed_all(es.MultipartEventParser(), payload, chunk=4096)
    for chunk in (1, 7, 13, 100, 529, 4096, 100000):
        got = _feed_all(es.MultipartEventParser(), payload, chunk=chunk)
        assert len(got) == len(baseline), f"chunk={chunk} 得到 {len(got)} 个事件"


def test_single_byte_chunks():
    """逐字节喂入也能正确解析（极端分割）。"""
    payload = _payload(DEVICE_LEVEL_65)
    events = _feed_all(es.MultipartEventParser(), payload, chunk=1)
    assert events


def test_incomplete_trailing_frame_buffered():
    """末尾不完整帧被缓冲，不丢弃也不误报。"""
    payload = _payload(NVR_64)
    cut = payload.rindex(b"--boundary")
    p = es.MultipartEventParser()
    complete = _feed_all(p, payload[:cut])
    # 补上被截断的尾帧
    rest = p.feed(payload[cut:])
    assert len(complete) + len(rest) == 46


# ── 3. 有界缓冲：消防水管防护 ──────────────────────────────────────


def test_buffer_bounded_on_flood_device():
    """176.10 的 2.25MB 洪流不得让缓冲无限增长。"""
    payload = _payload(FLOOD_10)
    assert len(payload) > 2_000_000, "洪流 fixture 应超过 2MB"
    p = es.MultipartEventParser(max_buffer_bytes=256 * 1024)
    # 一次性喂入全部 2.25MB
    p.feed(payload)
    assert p.buffer_size <= 256 * 1024, f"缓冲未受限: {p.buffer_size}"


def test_flood_still_yields_events():
    """有界缓冲下洪流仍能解析出事件（丢弃的是最旧字节，不是全部）。"""
    p = es.MultipartEventParser(max_buffer_bytes=256 * 1024)
    events = _feed_all(p, _payload(FLOOD_10))
    assert events, "洪流设备应仍能解析出事件"
    assert {e.event_type for e in events} <= {"VMD", "videoloss"}


def test_oldest_bytes_dropped_when_overflow():
    """溢出时丢弃最旧字节，并计入统计（可诊断，不静默）。"""
    p = es.MultipartEventParser(max_buffer_bytes=1024)
    # 喂入无边界标记的垃圾数据填满缓冲
    p.feed(b"x" * 5000)
    assert p.buffer_size <= 1024
    assert p.dropped_bytes >= 5000 - 1024


def test_per_feed_byte_cap():
    """单次 feed 的字节上限：超过则截断并计数（防止一次巨块阻塞循环）。"""
    p = es.MultipartEventParser(max_feed_bytes=1024)
    p.feed(b"y" * 10_000)
    assert p.dropped_bytes >= 10_000 - 1024


# ── 4. 真机通道多样性 ──────────────────────────────────────────────
#
# 去抖逻辑本身在 AlertStreamReader._dispatch（逐键 _last_state），由
# test_v08_event_stream_reader.py 覆盖。此处只验证解析出的真机通道集合，
# 因为逐通道事件传感器要按这些通道号建实体。
#
# ⚠️ 曾有 es.dedupe() 全局去抖函数：它会吞掉 active→inactive→active 的
# 第三次事件，导致运动传感器在恢复后永久失效。已删除，改由读取器按
# (channel_id, event_type) 记录末态，只抑制**连续重复**。


def test_real_payload_channel_diversity():
    """176.64 实测 6 个通道都推 VMD → 解析出的通道集合必须完整。"""
    p = es.MultipartEventParser()
    events = _feed_all(p, _payload(NVR_64))
    chans = {e.channel_id for e in events}
    assert chans == {"1", "2", "3", "10", "11", "12"}
    assert len(events) == 46


def test_real_payload_unique_state_keys():
    """实测唯一 (通道,类型,状态) 组合数 = 6，即逐键去抖后应派发 6 个事件。

    这是读取器 test_real_fixture_payload_dispatched 断言的数据依据。
    """
    p = es.MultipartEventParser()
    events = _feed_all(p, _payload(NVR_64))
    keys = {(e.channel_id, e.event_type, e.event_state) for e in events}
    assert len(keys) == 6


# ── 5. 容错 ────────────────────────────────────────────────────────
#
# 注意：构造 payload 必须带尾随分隔符。真机流中每个完整帧后面都跟着
# 下一帧的分隔符（176.64 实测 47 个分隔符 / 46 个完整帧），解析器据此
# 把末段视为"可能不完整的尾帧"留在缓冲。构造数据不带尾随分隔符会模拟
# 出真机不存在的形态。


def test_malformed_xml_frame_skipped():
    """坏帧跳过，不崩溃，后续帧仍解析。"""
    payload = (
        b"--boundary\r\nContent-Type: application/xml\r\n\r\n"
        b"<EventNotificationAlert><broken>\r\n"
        b"--boundary\r\nContent-Type: application/xml\r\n\r\n"
        b"<EventNotificationAlert><channelID>1</channelID>"
        b"<eventType>VMD</eventType><eventState>active</eventState>"
        b"</EventNotificationAlert>\r\n"
        b"--boundary\r\n"  # 尾随分隔符，让上一帧完整收尾
    )
    p = es.MultipartEventParser()
    events = _feed_all(p, payload)
    assert len(events) == 1
    assert events[0].event_type == "VMD"


def test_non_xml_frame_ignored():
    """非 XML 帧（部分固件发 JPEG 心跳）被忽略。

    实测依据：176.10 的 2.25MB 洪流里 25 个帧只有 10 个是 XML 事件，
    其余为二进制/JPEG 帧 —— 必须跳过而非崩溃。
    """
    payload = (
        b"--boundary\r\nContent-Type: image/jpeg\r\n\r\n"
        b"\xff\xd8\xff\xe0garbagejpegdata\r\n"
        b"--boundary\r\nContent-Type: application/xml\r\n\r\n"
        b"<EventNotificationAlert><eventType>videoloss</eventType>"
        b"<channelID>1</channelID><eventState>inactive</eventState>"
        b"</EventNotificationAlert>\r\n"
        b"--boundary\r\n"  # 尾随分隔符
    )
    p = es.MultipartEventParser()
    events = _feed_all(p, payload)
    assert len(events) == 1
    assert events[0].event_type == "videoloss"


def test_empty_and_garbage_input():
    """空输入/纯垃圾不抛异常，返回空列表。"""
    p = es.MultipartEventParser()
    assert p.feed(b"") == []
    assert p.feed(b"\r\n\r\n--boundary\r\n") == []
    assert p.feed(b"total garbage with no structure") == []


def test_partial_event_without_required_fields_skipped():
    """缺 eventType 的帧跳过（无法映射到任何实体）。"""
    payload = (
        b"--boundary\r\n\r\n"
        b"<EventNotificationAlert><channelID>1</channelID>"
        b"</EventNotificationAlert>\r\n"
        b"--boundary\r\n"  # 尾随分隔符
    )
    p = es.MultipartEventParser()
    assert _feed_all(p, payload) == []


def test_crlf_and_lf_both_accepted():
    """真机抓包是 LF，但 HTTP 标准是 CRLF，两者都要能解析。"""
    lf = (b"--boundary\nContent-Type: application/xml\n\n"
          b"<EventNotificationAlert><eventType>VMD</eventType>"
          b"<channelID>3</channelID><eventState>active</eventState>"
          b"</EventNotificationAlert>\n"
          b"--boundary\n")  # 尾随分隔符
    crlf = lf.replace(b"\n", b"\r\n")
    for payload in (lf, crlf):
        p = es.MultipartEventParser()
        events = _feed_all(p, payload)
        assert len(events) == 1, f"解析失败: {payload[:40]!r}"
        assert events[0].channel_id == "3"
