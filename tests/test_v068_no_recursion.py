"""Tests for v0.6.8 linear-flow auth retry (no recursion).

Pre-v0.6.8 the ISAPI client's auth-handshake branch recursively
called ``_read_response_text`` for the retry response. If the device
kept returning 401/403 (e.g. password contains non-UTF-8 bytes
trapping the Digest computation, or the server is misconfigured and
never accepts any auth), the recursion ran unbounded and Python
eventually crashed with a stack overflow. The user's HA logs showed
this happening 371+ times before the traceback cut off.

v0.6.8 fix: linear auth flow with explicit max one Digest retry. If
the Digest retry still returns 401, we raise ISAPIAuthError directly
instead of recursing.

Also tested: UnicodeEncodeError in the credentials (password with
lone surrogates) is caught and surfaced as ISAPIAuthError with a
human-readable message — was previously crashing inside
``_compute_digest_response`` at ``f"{username}:{realm}:{password}"
.encode("utf-8")`` with no recovery.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# v0.7.1: retired — same dead ``_session`` seam as test_v063.
#
# Behaviour coverage:
#   - surrogate / non-UTF-8 credential guard (the two
#     ``*_with_surrogates_*`` cases) → test_v071_credentials_and_put.py.
#     Worth noting this guard had actually REGRESSED: v0.6.8 added
#     ``_validate_credentials_encoding``, the v0.6.12 httpx migration
#     dropped it, and these tests could not catch that because their
#     patch never took effect. Restored in v0.7.1.
#   - no-recursion cases → structurally guaranteed now: ``_request`` has
#     exactly two ``self._client.request`` call sites (original + one
#     basic retry) and cannot recurse. Digest-vs-basic routing and the
#     sticky auth switch are pinned by test_v0612_httpx.py.
pytest.skip(
    "retired: patches the removed aiohttp ``_session`` seam; superseded by "
    "test_v0612_httpx.py and test_v071_credentials_and_put.py",
    allow_module_level=True,
)

from custom_components.hikvision_isapi_performance.isapi_client import (  # noqa: E402
    ISAPIAuthError,
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
            raise RuntimeError(
                f"session.request called more times than expected "
                f"(got {len(captured)} so far)"
            )

    session = MagicMock()
    session.request = request
    session.get = request
    return session


# ---- linear-flow guarantee: no infinite recursion ----


def test_get_text_basic_401_then_digest_401_does_not_recurse():
    """v0.6.8 fix: Basic + Digest both fail must terminate, not recurse.

    Pre-fix: 401 empty -> Basic -> 401 with Digest -> Digest retry
    -> 401 empty -> Basic -> ... infinite loop.
    Post-fix: 401 empty -> Basic (fails) -> Digest retry (fails) ->
    ISAPIAuthError. Exactly 3 requests: original + Basic + Digest.
    """
    captured: list[dict[str, Any]] = []
    session = _build_session(
        responses=[
            # Original
            _FakeRespCM(401, {"WWW-Authenticate": ""}),
            # Basic attempt
            _FakeRespCM(
                401,
                {"WWW-Authenticate": 'Digest realm="r", nonce="n", qop="auth", algorithm=MD5'},
            ),
            # Digest retry
            _FakeRespCM(401, {"WWW-Authenticate": ""}),
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
    # Three requests, NOT 371+ — the linear flow stops here.
    assert len(captured) == 3, (
        f"Expected exactly 3 requests (linear flow), got "
        f"{len(captured)}. This indicates recursion has returned."
    )


def test_get_text_digest_401_then_digest_401_does_not_recurse():
    """v0.6.8 fix: two Digest 401s in a row terminates immediately.

    Pre-fix: server sent Digest, retry 401, recurse, retry 401,
    recurse, ... infinite loop.
    Post-fix: server sent Digest -> retry 401 -> ISAPIAuthError.
    Exactly 2 requests.
    """
    captured: list[dict[str, Any]] = []
    session = _build_session(
        responses=[
            _FakeRespCM(
                401,
                {"WWW-Authenticate": 'Digest realm="r", nonce="n", qop="auth", algorithm=MD5'},
            ),
            _FakeRespCM(401, {"WWW-Authenticate": ""}),
        ],
        captured=captured,
    )

    client = ISAPIClient(host="x", username="admin", password="pw")
    client._session = session

    try:
        asyncio.run(client.get_text("/x"))
    except ISAPIAuthError:
        pass
    else:
        raise AssertionError("Expected ISAPIAuthError")
    assert len(captured) == 2, (
        f"Expected exactly 2 requests (linear flow), got "
        f"{len(captured)}. This indicates recursion has returned."
    )


def test_get_text_digest_success_path_still_works():
    """v0.6.8 refactor: the happy path (Digest succeeds) is unchanged."""
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
    assert len(captured) == 2
    assert captured[1]["headers"]["Authorization"].startswith("Digest ")


# ---- UTF-8 safety on credentials ----


def test_get_text_password_with_surrogates_raises_clean_auth_error():
    """v0.6.8 fix: password with lone surrogates must NOT crash
    inside _compute_digest_response. Pre-fix, this raised
    UnicodeEncodeError from the f-string ``.encode("utf-8")`` call
    inside Digest computation, then propagated up the recursive
    call stack (because the recursion couldn't catch a UnicodeError
    and re-raise as ISAPIAuthError, it bubbled all the way out and
    poisoned the coordinator's UpdateFailed chain).

    Post-fix: caught at the top of _read_response_text and re-raised
    as ISAPIAuthError with a clear message. Exactly 1 request
    issued (no auth retry on bad credentials).
    """
    captured: list[dict[str, Any]] = []
    # Lone surrogate: '\udcff' cannot be encoded to UTF-8
    bad_password = "abc\udcff"
    session = _build_session(
        responses=[_FakeRespCM(401, {"WWW-Authenticate": ""})],
        captured=captured,
    )

    client = ISAPIClient(host="x", username="admin", password=bad_password)
    client._session = session

    try:
        asyncio.run(client.get_text("/x"))
    except ISAPIAuthError as exc:
        msg = str(exc)
        assert "invalid UTF-8" in msg or "non-UTF-8" in msg, (
            f"Expected helpful UTF-8 error message, got: {msg}"
        )
    except UnicodeEncodeError:
        raise AssertionError(
            "UnicodeEncodeError leaked through; v0.6.8 should have "
            "caught it and re-raised as ISAPIAuthError."
        )
    else:
        raise AssertionError("Expected ISAPIAuthError")
    # Only the original request — no retry with bad creds.
    assert len(captured) == 1


def test_get_text_username_with_surrogates_raises_clean_auth_error():
    """Same fix: bad username (lone surrogate) also caught."""
    bad_username = "ad\udcfemin"
    session = _build_session(
        responses=[_FakeRespCM(401, {"WWW-Authenticate": ""})],
    )

    client = ISAPIClient(host="x", username=bad_username, password="pw")
    client._session = _build_session(responses=[_FakeRespCM(401, {"WWW-Authenticate": ""})])

    try:
        asyncio.run(client.get_text("/x"))
    except ISAPIAuthError:
        pass
    except UnicodeEncodeError:
        raise AssertionError("UnicodeEncodeError leaked through")
    else:
        raise AssertionError("Expected ISAPIAuthError")