"""pytest 插件：测试期间禁止任何真实 socket 连接。

用于证明测试套件已完全离线。用法:
    python -m pytest tests/ -p no:cacheprovider -p network_guard
"""

from __future__ import annotations

import socket

import pytest

# 允许的连接目标（本地回环，用于 unix socket / 本地 IPC）
_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection

_violations: list[str] = []


def _describe(target) -> str:
    try:
        return str(target)
    except Exception:  # noqa: BLE001
        return "<undisplayable>"


def _is_allowed(target) -> bool:
    host = None
    if isinstance(target, tuple) and target:
        host = target[0]
    elif isinstance(target, (str, bytes)):
        host = target
    if host is None:
        return False
    if isinstance(host, bytes):
        host = host.decode("utf-8", errors="replace")
    return str(host) in _ALLOWED_HOSTS


def _guarded_connect(self, target):
    if not _is_allowed(target):
        _violations.append(_describe(target))
        raise AssertionError(
            f"测试尝试发起真实网络连接: {target!r} — 测试必须离线"
        )
    return _real_connect(self, target)


def _guarded_connect_ex(self, target):
    if not _is_allowed(target):
        _violations.append(_describe(target))
        raise AssertionError(
            f"测试尝试发起真实网络连接: {target!r} — 测试必须离线"
        )
    return _real_connect_ex(self, target)


def _guarded_create_connection(address, *args, **kwargs):
    if not _is_allowed(address):
        _violations.append(_describe(address))
        raise AssertionError(
            f"测试尝试发起真实网络连接: {address!r} — 测试必须离线"
        )
    return _real_create_connection(address, *args, **kwargs)


def pytest_configure(config):
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.create_connection = _guarded_create_connection


def pytest_unconfigure(config):
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex
    socket.create_connection = _real_create_connection
    if _violations:
        print(f"\n!! 检测到 {len(_violations)} 次真实网络连接尝试:")
        for v in sorted(set(_violations)):
            print(f"     {v}")
    else:
        print("\n[OK] 整个测试套件未发起任何真实网络连接")
