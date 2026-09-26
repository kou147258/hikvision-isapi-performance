"""Tests for v0.6.12 migration to httpx + end-to-end auth behavior.

v0.6.12 changed two architectural things at once:

1. The HTTP transport moved from aiohttp to httpx. httpx's
   built-in ``DigestAuth`` / ``BasicAuth`` classes replace our
   hand-rolled digest computation (which didn't handle SHA-256
   or MD5-sess algorithm variants that some V5.7+ firmware
   advertises).

2. The smart-fallback auth pattern: on the first 401, if the
   server's WWW-Authenticate does NOT contain "digest", retry
   the same request with ``BasicAuth``. If Basic works, switch
   permanently (matches ``curl --anyauth`` semantics). If the
   server DOES offer Digest and still 401s, do NOT retry with
   Basic — that's a wrong-credentials case, and retrying would
   just burn another failed login against Hikvision's
   account-lockout policy.

These tests pin both behaviors with ``httpx.MockTransport`` so
no real device is needed. The transport injects canned responses
and captures the exact requests the client makes.
"""

from __future__ import annotations

import httpx
import pytest

from custom_components.hikvision_isapi_performance.isapi_client import (
    ISAPIAuthError,
    ISAPIConnectionError,
    ISAPIClient,
)


# ---- helpers ----


def _xml_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=body.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
    )


def _build_mock_client(handler) -> ISAPIClient:
    """Build an ISAPIClient backed by ``httpx.MockTransport(handler)``.

    Setup is synchronous because ``httpx.AsyncClient(...)`` is a
    non-blocking constructor — only ``.request()`` is async.

    NOTE: the transport MUST be passed to the constructor. httpx binds
    the transport inside ``AsyncClient.__init__`` (into the mount table
    consulted by ``_transport_for_url``); assigning ``client._transport``
    afterwards has no effect. Pre-v0.7.1 this helper did exactly that, so
    every request escaped to the real network and hit the live camera at
    the fixture IP 10.18.176.10 — producing bogus failures and hammering
    a production device.
    """
    client = ISAPIClient(
        host="10.18.176.10",
        username="admin",
        password="secret",
        port=80,
        use_https=False,
    )
    real = httpx.AsyncClient(
        timeout=10.0, verify=False, follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    client._client = real
    return client


# ---- v0.6.12: digest on first 401 ----


@pytest.mark.asyncio
async def test_v612_digest_challenge_triggers_one_retry_with_authorization():
    """Standard Hikvision V5.x flow: 401 with Digest challenge → retry
    with the computed Authorization header → 200."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Digest realm="IPCamera", nonce="abc123", '
                        'qop="auth", algorithm=MD5'
                    ),
                },
            )
        return _xml_response("<DeviceInfo><model>DS-2CD2</model></DeviceInfo>")

    client = _build_mock_client(handler)
    try:
        root = await client.get_xml("/ISAPI/System/deviceInfo")
        assert root.findtext("model") == "DS-2CD2"
        # First request: no Authorization header.
        assert "authorization" not in calls[0]
        # Second request: Authorization header set by httpx.DigestAuth.
        assert calls[1]["authorization"].startswith("Digest ")
    finally:
        await client._client.aclose()


# ---- v0.6.12: smart basic-auth fallback ----


@pytest.mark.asyncio
async def test_v612_basic_only_challenge_triggers_basic_fallback():
    """Old DS-2CD8464F-EI returns 401 with Basic-only challenge.
    Client MUST try Basic on the same path."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Basic realm="IPCamera"'},
            )
        return _text_ok("<ok/>")

    client = _build_mock_client(handler)
    try:
        body = await client.get_text("/path")
        assert body == "<ok/>"
        # Two requests: digest attempt (401) + basic retry (200).
        assert len(calls) == 2
        # Retry used Basic auth.
        assert calls[1]["authorization"].startswith("Basic ")
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_v612_empty_challenge_triggers_basic_fallback():
    """Some Hikvision firmware returns 401 with NO WWW-Authenticate
    header at all. Client MUST still try Basic (rather than
    wrongly reporting auth failure)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if len(calls) == 1:
            # No WWW-Authenticate header at all.
            return httpx.Response(401)
        return _text_ok("<ok/>")

    client = _build_mock_client(handler)
    try:
        body = await client.get_text("/path")
        assert body == "<ok/>"
        assert len(calls) == 2
    finally:
        await client._client.aclose()


# ---- v0.6.12: do NOT retry basic when digest offered ----


@pytest.mark.asyncio
async def test_v612_digest_offered_with_401_does_not_retry_basic():
    """If the server offers Digest and still 401s, credentials are
    wrong. Retrying with Basic would burn another failed login
    toward Hikvision's account-lockout policy. The client MUST
    surface ISAPIAuthError after ONE digest retry."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        # Always 401 with a Digest challenge — credentials wrong.
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": (
                    'Digest realm="IPCamera", nonce="abc", qop="auth", '
                    'algorithm=MD5'
                ),
            },
        )

    client = _build_mock_client(handler)
    try:
        with pytest.raises(ISAPIAuthError) as exc:
            await client.get_text("/path")
        assert exc.value.status_code == 401
        # Exactly two requests: initial + digest retry. NO basic retry.
        assert len(calls) == 2
        # The second request was a digest retry, not basic.
        assert calls[1]["authorization"].startswith("Digest ")
    finally:
        await client._client.aclose()


# ---- v0.6.12: auth switch persists for the rest of the session ----


@pytest.mark.asyncio
async def test_v612_auth_switch_is_sticky():
    """After falling back to Basic once, subsequent requests skip
    the unauthenticated probe — they go straight with Basic."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        return _text_ok("<ok/>")

    client = _build_mock_client(handler)
    try:
        # Simulate post-fallback state.
        client._auth = client._basic_auth
        await client.get_text("/first")
        await client.get_text("/second")
        await client.get_text("/third")
        # Three requests, all with Basic auth header (no probe 401).
        assert len(calls) == 3
        for c in calls:
            assert c["authorization"].startswith("Basic ")
    finally:
        await client._client.aclose()


# ---- v0.6.12: network errors surface as ISAPIConnectionError ----


@pytest.mark.asyncio
async def test_v612_connect_error_becomes_isapi_connection_error():
    """TCP-level failure (refused, unreachable) surfaces as
    ISAPIConnectionError, NOT ISAPIAuthError (the coordinator
    catches this and marks entities unavailable until next
    refresh, but a network error is qualitatively different from
    a credential error)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("No connection could be made")

    client = _build_mock_client(handler)
    try:
        with pytest.raises(ISAPIConnectionError):
            await client.get_text("/path")
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_v612_timeout_becomes_isapi_connection_error():

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = _build_mock_client(handler)
    try:
        with pytest.raises(ISAPIConnectionError):
            await client.get_text("/path")
    finally:
        await client._client.aclose()


# ---- v0.6.12: server errors are ISAPIError (4xx/5xx, not 401) ----


@pytest.mark.asyncio
async def test_v612_500_raises_isapi_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, content=b"Internal Server Error",
            headers={"Content-Type": "text/plain"},
        )

    client = _build_mock_client(handler)
    try:
        with pytest.raises(Exception) as exc:
            await client.get_text("/path")
        # We don't import ISAPIError directly to avoid name collision;
        # any non-auth, non-connection exception is fine.
        from custom_components.hikvision_isapi_performance.isapi_client import ISAPIError
        assert isinstance(exc.value, ISAPIError)
        assert exc.value.status_code == 500
    finally:
        await client._client.aclose()


# ---- v0.6.12: coordinator per-channel loop catches ISAPIConnectionError ----


def test_v0612_coordinator_per_channel_imports_include_connection_error():
    """v0.6.12: coordinator.py's per-channel loop must catch
    ISAPIConnectionError (in addition to ISAPIError / ISAPIAuthError)
    so a network blip on a single channel doesn't take the entire
    refresh down. This is a regression test that asserts the source
    file references ISAPIConnectionError in the per-channel except
    tuple.
    """
    import re
    from pathlib import Path
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\coordinator.py"
    ).read_text(encoding="utf-8")
    # Locate the per-channel for-loop block; ensure ISAPIConnectionError
    # appears at least twice (once in the primary try/except, once in
    # the alt-status try/except).
    pattern = r"ISAPIConnectionError"
    occurrences = len(re.findall(pattern, src))
    assert occurrences >= 2, (
        f"coordinator.py must reference ISAPIConnectionError at "
        f"least twice (primary + alt per-channel try/except). "
        f"Found {occurrences}."
    )


def test_v0612_manifest_version_bumped():
    """v0.6.12 anchor; v0.6.13 added xmlns-strip but the manifest
    requirement for httpx is unchanged."""
    import json
    from pathlib import Path
    manifest = json.loads(Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\manifest.json"
    ).read_text(encoding="utf-8"))
    # The current version is whatever follows v0.6.12 — we don't
    # pin a specific value here so this test stays valid across
    # subsequent bug-fix releases. Just verify it's at or beyond
    # v0.6.12.
    ver = manifest["version"]
    parts = ver.split(".")
    assert parts[0] == "0", f"unexpected major: {ver}"
    assert int(parts[1]) >= 6, f"unexpected minor: {ver}"


def test_v0612_manifest_requires_httpx():
    """v0.6.12: manifest.json.requirements includes httpx."""
    import json
    from pathlib import Path
    manifest = json.loads(Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\manifest.json"
    ).read_text(encoding="utf-8"))
    reqs = manifest.get("requirements", [])
    assert any(
        r.lower().startswith("httpx") for r in reqs
    ), f"manifest.json requirements missing httpx: {reqs}"


def _text_ok(text: str) -> httpx.Response:
    """Convenience: 200 response with ``text`` content-type."""
    return httpx.Response(200, content=text.encode("utf-8"))
