"""Tests for v0.6.7 Basic-auth fallback + optional channels endpoint.

Pre-v0.6.7 the digest-only ISAPI client crashed on devices that
return 401/403 without a Digest challenge (verified against the
user's 10.18.176.10 / 10.18.176.65 fleet on
/ISAPI/ContentMgmt/InputProxy/channels). The 403 had no
WWW-Authenticate header at all, so ``_parse_digest_challenge``
returned None and the client raised ISAPIAuthError. The
coordinator's ``except ISAPIAuthError`` clause caught it
(v0.6.5 fix) but turned into UpdateFailed, marking every entity
unavailable.

v0.6.7 fix has two parts:

1. ``isapi_client.py``: when WWW-Authenticate is missing or not
   Digest, retry with Basic auth. Some Hikvision firmware (older
   V4.x NVRs, early IPCs) accepts Basic when Digest is
   misconfigured server-side.

2. ``coordinator.py``: make the channels GET optional. Even with
   Basic auth, if the device still returns 403 we don't want to
   abort the refresh — deviceInfo + system_status data is enough
   to keep sensors populated. The channels list just becomes
   empty, which means no per-channel entities (cameras, switches,
   per-channel binary sensors) — better than a full
   coordinator-failed state.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# v0.7.1: this module is retired, not merely failing.
#
# It targets the pre-v0.6.12 aiohttp implementation: it imports
# ``_build_basic_header`` (deleted when auth moved to httpx's built-in
# ``DigestAuth``/``BasicAuth``) and patches ``client._session``, an
# attribute the httpx client never had — it uses ``client._client``.
# Both seams silently did nothing, so every test here issued REAL HTTP
# requests to the fixture IP 10.18.176.10.
#
# The broken import also aborted pytest collection for the entire suite,
# so not one test ran.
#
# Behaviour coverage moved to:
#   tests/test_v0612_httpx.py           — digest/basic routing, sticky switch
#   tests/test_v071_credentials_and_put.py — credential guard, PUT body replay
pytest.skip(
    "retired: targets the pre-v0.6.12 aiohttp client; superseded by "
    "test_v0612_httpx.py and test_v071_credentials_and_put.py",
    allow_module_level=True,
)

from custom_components.hikvision_isapi_performance.isapi_client import (  # noqa: E402
    ISAPIClient,
)


class _FakeRespCM:
    def __init__(self, status: int, headers: dict, body_text: str = ""):
        self._resp = MagicMock()
        self._resp.status = status
        self._resp.headers = headers
        self._resp.url = MagicMock()
        self._resp.url.__str__ = lambda _: "https://10.18.176.10:443/x"
        self._resp.text = AsyncMock(return_value=body_text)
        self._resp.read = AsyncMock(return_value=body_text.encode("utf-8"))

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return None


def _build_session(responses, captured=None):
    iter_index = iter(responses)

    def request(method, url, data=None, headers=None):
        if captured is not None:
            captured.append({
                "method": method, "url": url, "data": data,
                "headers": dict(headers or {}),
            })
        try:
            return next(iter_index)
        except StopIteration:
            raise RuntimeError("session.request called more times than expected")

    session = MagicMock()
    session.request = request
    return session


def test_build_basic_header_encodes_credentials():
    """Basic auth must base64-encode ``username:password``."""
    header = _build_basic_header("admin", "secret")
    expected = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")
    assert header == expected


def test_get_text_basic_auth_fallback_on_empty_www_authenticate():
    """v0.6.7 fix: 403 + empty WWW-Authenticate triggers Basic auth fallback.

    First call: 403 + empty WWW-Authenticate (Hikvision quirk on
    /InputProxy/channels). The client should retry using Basic auth
    instead of raising ISAPIAuthError.

    Second call: 200 OK with valid XML body.
    """
    captured: list[dict[str, Any]] = []
    session = _build_session(
        responses=[
            _FakeRespCM(403, {"WWW-Authenticate": ""}),
            _FakeRespCM(200, {}, body_text="<root>ok</root>"),
        ],
        captured=captured,
    )

    client = ISAPIClient(host="10.18.176.10", username="admin", password="x")
    client._session = session

    text = asyncio.run(client.get_text("/ISAPI/ContentMgmt/InputProxy/channels"))
    assert text == "<root>ok</root>"
    assert len(captured) == 2
    # First call had no Authorization
    assert "Authorization" not in captured[0]["headers"]
    # Second call used Basic auth
    assert captured[1]["headers"].get("Authorization", "").startswith("Basic ")


def test_get_text_basic_auth_fallback_on_basic_challenge():
    """v0.6.7 fix: explicit ``WWW-Authenticate: Basic`` also works."""
    captured: list[dict[str, Any]] = []
    session = _build_session(
        responses=[
            _FakeRespCM(
                401,
                {"WWW-Authenticate": 'Basic realm="IPCamera"'},
            ),
            _FakeRespCM(200, {}, body_text="<root>ok</root>"),
        ],
        captured=captured,
    )

    client = ISAPIClient(host="x", username="admin", password="pw")
    client._session = session

    text = asyncio.run(client.get_text("/x"))
    assert text == "<root>ok</root>"
    assert len(captured) == 2
    assert captured[1]["headers"]["Authorization"].startswith("Basic ")


def test_get_text_digest_still_works_after_basic_fallback_added():
    """v0.6.7 fix: Digest handshake path is unchanged."""
    captured: list[dict[str, Any]] = []
    session = _build_session(
        responses=[
            _FakeRespCM(
                401,
                {"WWW-Authenticate": 'Digest realm="r", nonce="n", qop="auth", algorithm=MD5'},
            ),
            _FakeRespCM(200, {}, body_text="<root>ok</root>"),
        ],
        captured=captured,
    )

    client = ISAPIClient(host="x", username="admin", password="pw")
    client._session = session

    text = asyncio.run(client.get_text("/x"))
    assert text == "<root>ok</root>"
    # Should be Digest (not Basic)
    assert captured[1]["headers"]["Authorization"].startswith("Digest ")


def test_get_text_basic_and_digest_both_fail_raises_auth_error():
    """If both Basic and Digest fail, raise ISAPIAuthError."""
    from custom_components.hikvision_isapi_performance.isapi_client import (
        ISAPIAuthError,
    )

    captured: list[dict[str, Any]] = []
    session = _build_session(
        responses=[
            # First 403, empty header — triggers Basic fallback
            _FakeRespCM(403, {"WWW-Authenticate": ""}),
            # Basic attempt: still 403, no Digest challenge either
            _FakeRespCM(403, {"WWW-Authenticate": ""}),
        ],
        captured=captured,
    )

    client = ISAPIClient(host="x", username="admin", password="pw")
    client._session = session

    try:
        asyncio.run(client.get_text("/x"))
    except ISAPIAuthError:
        pass  # expected
    else:
        raise AssertionError("Expected ISAPIAuthError")
    # First was no auth, second was Basic
    assert len(captured) == 2
    assert captured[1]["headers"]["Authorization"].startswith("Basic ")