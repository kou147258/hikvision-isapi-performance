"""v0.8 步骤8b：AlertStreamReader（alertStream 长连接读取器）。

解析器是纯函数，本模块负责网络侧：长连接、重连退避、取消安全、
把事件派发给回调（供二值传感器更新 + HA 事件总线）。

设计要求（来自实测与竞品调研）：
  * 12/12 台可用，但 176.18 只接受 Basic → 必须复用客户端的认证协商结果
  * 176.10 是消防水管（281 KB/s）→ 必须用有界缓冲解析器
  * 竞品集成的教训：alertStream 一旦网络抖动就永久断开直到 HA 重启
    → 必须实现指数退避重连
  * 不得阻塞事件循环 → 用 httpx aiter_bytes 分块读
  * HA 卸载集成时必须干净关闭 → stop() 取消任务并关闭连接，不留孤儿

测试用可注入的假流工厂，零真实网络。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance import event_stream as es  # noqa: E402
from custom_components.hikvision_isapi_performance.isapi_client import (  # noqa: E402
    ISAPIAuthError,
    ISAPIConnectionError,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures_v08"


def _frame(ch: str, et: str, st: str, name: str = "") -> bytes:
    name_xml = f"<channelName>{name}</channelName>" if name else ""
    head = "--boundary\r\nContent-Type: application/xml\r\n\r\n"
    body = (
        f"<EventNotificationAlert><channelID>{ch}</channelID>"
        f"<eventType>{et}</eventType><eventState>{st}</eventState>"
        f"{name_xml}</EventNotificationAlert>\r\n"
        "--boundary\r\n"
    )
    return (head + body).encode("utf-8")


class _FakeStreamClient:
    """假客户端：stream_bytes 按预设脚本产出字节或抛异常。

    script 是 list，每项是 bytes（产出后结束）或 Exception（抛出）。
    每次调用 stream_bytes 消费一项，记录调用次数供断言。
    """

    def __init__(self, script, *, chunk_size=64):
        self.script = list(script)
        self.chunk_size = chunk_size
        self.connect_count = 0
        self.closed = False

    async def stream_bytes(self, path):
        self.connect_count += 1
        if not self.script:
            # 脚本耗尽：模拟持续空流，让测试可控地停止
            await asyncio.sleep(3600)
            return
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        for i in range(0, len(item), self.chunk_size):
            yield item[i:i + self.chunk_size]

    async def aclose(self):
        self.closed = True


def _make_reader(client, on_event, **kwargs):
    kwargs.setdefault("reconnect_initial", 0.01)
    kwargs.setdefault("reconnect_max", 0.05)
    return es.AlertStreamReader(
        client_factory=lambda: client,
        on_event=on_event,
        **kwargs,
    )


# ── 1. 事件派发 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatches_parsed_events():
    """流里的事件被解析并派发到回调。"""
    got = []
    client = _FakeStreamClient([_frame("1", "VMD", "active", "摄像机12")])
    reader = _make_reader(client, got.append)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.05)
    await reader.stop(task)

    assert got, "应派发事件"
    e = got[0]
    assert e.channel_id == "1"
    assert e.event_type == "VMD"
    assert e.channel_name == "摄像机12"


@pytest.mark.asyncio
async def test_dispatches_multiple_events():
    client = _FakeStreamClient([
        _frame("1", "VMD", "active"),
        _frame("2", "videoloss", "inactive"),
    ])
    got = []
    reader = _make_reader(client, got.append)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.05)
    await reader.stop(task)
    assert {(e.channel_id, e.event_type) for e in got} == {
        ("1", "VMD"), ("2", "videoloss")}


@pytest.mark.asyncio
async def test_dedupe_applied_before_dispatch():
    """重复的相同状态只派发一次（防止事件总线被洪流打爆）。"""
    same = _frame("1", "VMD", "active")
    client = _FakeStreamClient([same * 20])
    got = []
    reader = _make_reader(client, got.append)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.05)
    await reader.stop(task)
    assert len(got) == 1, f"20 个重复事件应去抖为 1，实得 {len(got)}"


@pytest.mark.asyncio
async def test_state_transition_dispatched():
    """active→inactive 是状态变化，两次都派发。"""
    client = _FakeStreamClient([
        _frame("1", "VMD", "active"),
        _frame("1", "VMD", "inactive"),
    ])
    got = []
    reader = _make_reader(client, got.append)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.05)
    await reader.stop(task)
    assert [e.event_state for e in got] == ["active", "inactive"]


@pytest.mark.asyncio
async def test_real_fixture_payload_dispatched():
    """用真机抓包（176.64 的 46 个事件）验证端到端派发。"""
    raw = (FIXTURES / "10_18_176_64__F_alert__alertStream.xml").read_text(
        encoding="utf-8", errors="replace")
    lines = raw.split("\n")
    if lines and lines[0].startswith("[") and lines[0].endswith("]"):
        lines = lines[1:]
    payload = "\n".join(lines).encode("utf-8")

    client = _FakeStreamClient([payload], chunk_size=512)
    got = []
    reader = _make_reader(client, got.append)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.15)
    await reader.stop(task)

    # 46 个原始事件，6 个唯一 (通道,类型,状态) → 去抖后 6 个
    assert len(got) == 6, f"应派发 6 个去抖事件，实得 {len(got)}"


# ── 2. 重连与退避 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconnects_after_stream_ends():
    """流正常结束后重连（竞品集成的已知痛点：断开后永不重连）。"""
    client = _FakeStreamClient([
        _frame("1", "VMD", "active"),
        _frame("2", "VMD", "active"),
        _frame("3", "VMD", "active"),
    ])
    got = []
    reader = _make_reader(client, got.append)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.2)
    await reader.stop(task)
    assert client.connect_count >= 2, f"应重连，实连 {client.connect_count} 次"
    assert len({e.channel_id for e in got}) == 3


@pytest.mark.asyncio
async def test_reconnects_after_connection_error():
    """连接错误后重连，不崩溃退出。"""
    client = _FakeStreamClient([
        ISAPIConnectionError("network down"),
        _frame("1", "VMD", "active"),
    ])
    got = []
    reader = _make_reader(client, got.append)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.2)
    await reader.stop(task)
    assert got, "重连后应能派发事件"


@pytest.mark.asyncio
async def test_reconnects_after_auth_error():
    """认证错误（403）也退避重连，不进入无延迟的忙循环。"""
    client = _FakeStreamClient([
        ISAPIAuthError("403", status_code=403),
        _frame("1", "VMD", "active"),
    ])
    got = []
    reader = _make_reader(client, got.append)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.2)
    await reader.stop(task)
    assert client.connect_count >= 2


@pytest.mark.asyncio
async def test_backoff_increases_on_repeated_failure():
    """连续失败时退避间隔增长（指数退避，有上限）。"""
    errors = [ISAPIConnectionError("down")] * 4
    client = _FakeStreamClient(errors)
    reader = _make_reader(client, lambda e: None,
                          reconnect_initial=0.01, reconnect_max=1.0)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.12)
    await reader.stop(task)
    delays = reader.backoff_history
    assert len(delays) >= 2, f"应记录多次退避: {delays}"
    assert delays == sorted(delays), f"退避应单调不减: {delays}"
    assert delays[-1] > delays[0], "退避应随失败增长"
    assert all(d <= 1.0 for d in delays), "退避不得超过上限"


@pytest.mark.asyncio
async def test_backoff_resets_after_successful_events():
    """成功收到事件后退避归零（避免长期运行后退避过大）。"""
    client = _FakeStreamClient([
        ISAPIConnectionError("down"),
        ISAPIConnectionError("down"),
        _frame("1", "VMD", "active"),
    ])
    reader = _make_reader(client, lambda e: None,
                          reconnect_initial=0.01, reconnect_max=1.0)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.25)
    await reader.stop(task)
    assert reader.current_backoff == reader.reconnect_initial


# ── 3. 取消与关闭安全 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_cancels_task():
    """stop() 取消任务，任务确实结束（不留孤儿）。"""
    client = _FakeStreamClient([_frame("1", "VMD", "active")])
    reader = _make_reader(client, lambda e: None)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.05)
    await reader.stop(task)
    assert task.done(), "任务应已结束"
    assert reader.stopped is True


@pytest.mark.asyncio
async def test_stop_closes_client():
    """stop() 关闭底层连接（避免连接泄漏）。"""
    client = _FakeStreamClient([_frame("1", "VMD", "active")])
    reader = _make_reader(client, lambda e: None)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.05)
    await reader.stop(task)
    assert client.closed is True


@pytest.mark.asyncio
async def test_no_events_dispatched_after_stop():
    """停止后不再派发事件（防止实体更新已卸载的 coordinator）。"""
    client = _FakeStreamClient([
        _frame("1", "VMD", "active"),
        _frame("2", "VMD", "active"),
        _frame("3", "VMD", "active"),
    ])
    got = []
    reader = _make_reader(client, got.append)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.05)
    await reader.stop(task)
    count_at_stop = len(got)
    await asyncio.sleep(0.1)
    assert len(got) == count_at_stop, "停止后仍在派发事件"


@pytest.mark.asyncio
async def test_cancellation_propagates_not_swallowed():
    """CancelledError 必须向上传播，不得被 except Exception 吞掉。

    吞掉 CancelledError 会导致 HA 无法正常卸载集成（任务永不结束）。
    """
    client = _FakeStreamClient([])  # 空流 → 永久 sleep
    reader = _make_reader(client, lambda e: None)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── 4. 有界缓冲与异常回调 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_flood_device_does_not_grow_unbounded():
    """消防水管设备（176.10 的 2.1MB）不得让缓冲无限增长。"""
    payload = (FIXTURES / "10_18_176_10__F_alert__alertStream.xml").read_bytes()
    assert len(payload) > 2_000_000
    client = _FakeStreamClient([payload], chunk_size=65536)
    reader = _make_reader(client, lambda e: None, max_buffer_bytes=128 * 1024)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.3)
    await reader.stop(task)
    assert reader.parser.buffer_size <= 128 * 1024


@pytest.mark.asyncio
async def test_callback_exception_does_not_kill_reader():
    """回调抛异常不得终止读取循环（一个坏实体不该拖垮整条流）。"""
    calls = []

    def bad_callback(e):
        calls.append(e)
        if len(calls) == 1:
            raise RuntimeError("entity exploded")

    client = _FakeStreamClient([
        _frame("1", "VMD", "active"),
        _frame("2", "VMD", "active"),
    ])
    reader = _make_reader(client, bad_callback)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.1)
    await reader.stop(task)
    assert len(calls) >= 2, "回调异常后应继续派发后续事件"


@pytest.mark.asyncio
async def test_parser_dropped_bytes_exposed():
    """丢弃字节数可观测（诊断用，不静默）。"""
    payload = (FIXTURES / "10_18_176_10__F_alert__alertStream.xml").read_bytes()
    client = _FakeStreamClient([payload], chunk_size=65536)
    reader = _make_reader(client, lambda e: None, max_buffer_bytes=64 * 1024)
    task = asyncio.create_task(reader.run())
    await asyncio.sleep(0.3)
    await reader.stop(task)
    assert reader.parser.dropped_bytes > 0, "洪流下应有丢弃字节被记录"
