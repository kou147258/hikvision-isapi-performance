"""v0.7.1: credential-encoding guard + PUT body preservation.

Replaces the aiohttp-era tests in ``test_v063_digest_retry.py`` /
``test_v068_no_recursion.py``, which patched a ``client._session``
attribute that has not existed since the v0.6.12 httpx migration (the
httpx client uses ``client._client``). Those patches were silently
ignored, so the tests issued real HTTP requests to the fixture IP and
failed against a live device.

Only the behaviours not already covered elsewhere are carried over:

* surrogate / non-UTF-8 credential guard (restored in v0.7.1 after the
  v0.6.8 ``_validate_credentials_encoding`` was lost in v0.6.12);
* ``put_text`` replays the original body and Content-Type when httpx
  retries the request with the computed Digest Authorization header.

The remaining v0.6.3/v0.6.8 cases (recursion termination, digest-vs-basic
challenge routing, sticky auth switch, NameError regression) are
structurally guaranteed by the current two-call ``_request`` flow and are
already pinned by ``test_v0612_httpx.py``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from custom_components.hikvision_isapi_performance.isapi_client import (
    ISAPIAuthError,
    ISAPIClient,
)

# Test fixture host. Never contacted: every test swaps in a MockTransport.
HOST = "10.18.176.10"

DIGEST_CHALLENGE = (
    'Digest realm="IPCamera", nonce="abc", qop="auth", algorithm=MD5'
)


def _client_with_transport(handler) -> ISAPIClient:
    """Build an ISAPIClient whose httpx client uses ``MockTransport``.

    The transport must be passed to the ``AsyncClient`` constructor:
    httpx binds it inside ``__init__`` into the mount table consulted by
    ``_transport_for_url``, and ignores any later assignment to the
    private ``_transport`` attribute.
    """
    client = ISAPIClient(
        host=HOST,
        username="admin",
        password="secret",
        port=80,
        use_https=False,
    )
    client._client = httpx.AsyncClient(
        timeout=10.0,
        verify=False,
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    return client


# ── credential encoding guard ─────────────────────────────────────────


def test_password_with_lone_surrogate_raises_auth_error():
    """A lone surrogate in the password raises ISAPIAuthError, not
    UnicodeEncodeError.

    ``httpx.DigestAuth.__init__`` calls ``to_bytes(password)`` eagerly,
    so without the guard this raised a bare codec error while merely
    constructing the client — killing every entity for the config entry
    with an inscrutable traceback.
    """
    with pytest.raises(ISAPIAuthError) as exc:
        ISAPIClient(host=HOST, username="admin", password="pass\ud800word")
    msg = str(exc.value)
    assert "password" in msg
    assert "UTF-8" in msg


def test_username_with_lone_surrogate_raises_auth_error():
    """Same guard applies to the username."""
    with pytest.raises(ISAPIAuthError) as exc:
        ISAPIClient(host=HOST, username="ad\udc00min", password="secret")
    assert "username" in str(exc.value)


def test_surrogate_error_message_does_not_leak_credential():
    """The message must not echo the offending value into the HA log.

    The literal field name (``password``) is expected in the message —
    what must never appear is the credential itself.
    """
    secret = "hunter2\ud800secret"
    with pytest.raises(ISAPIAuthError) as exc:
        ISAPIClient(host=HOST, username="admin", password=secret)
    msg = str(exc.value)
    assert secret not in msg
    assert "hunter2" not in msg
    assert "password" in msg  # field name is fine, value is not


def test_valid_credentials_construct_without_error():
    """Non-ASCII but valid UTF-8 credentials are accepted unchanged."""
    client = ISAPIClient(host=HOST, username="admin", password="密码123")
    assert client._digest_auth is not None
    assert client._basic_auth is not None


# ── PUT body / Content-Type preservation ──────────────────────────────


@pytest.mark.asyncio
async def test_put_text_preserves_body_and_content_type_across_digest_retry():
    """httpx's Digest retry must replay the original body + Content-Type.

    The device answers the first PUT with a 401 challenge; httpx then
    re-sends the same request with an Authorization header. Both attempts
    must carry the body — a regression here would silently send an empty
    PUT, which Hikvision answers with ``badXmlContent``.

    Snapshots are taken INSIDE the handler: httpx's ``DigestAuth``
    reuses and mutates the same ``Request`` object for the retry, so
    inspecting it after the fact would show the final state for both
    attempts.
    """
    snapshots: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        snapshots.append({
            "body": request.content.decode("utf-8"),
            "content_type": request.headers.get("content-type", ""),
            "authorization": request.headers.get("authorization", ""),
        })
        if len(snapshots) == 1:
            return httpx.Response(
                401, headers={"WWW-Authenticate": DIGEST_CHALLENGE},
            )
        return httpx.Response(
            200,
            content=b"<ResponseStatus><statusCode>1</statusCode></ResponseStatus>",
            headers={"Content-Type": "application/xml"},
        )

    client = _client_with_transport(handler)
    try:
        body = "<PTZData><continuous><direction>up</direction></continuous></PTZData>"
        text = await client.put_text("/ISAPI/PTZCtrl/channels/1/continuous", body)

        assert "<ResponseStatus>" in text
        assert len(snapshots) == 2

        # First attempt: body + Content-Type present, no Authorization yet.
        assert snapshots[0]["body"] == body
        assert "application/xml" in snapshots[0]["content_type"]
        assert snapshots[0]["authorization"] == ""

        # Retry: Authorization added, body still intact.
        assert snapshots[1]["authorization"].startswith("Digest ")
        assert snapshots[1]["body"] == body
        assert "application/xml" in snapshots[1]["content_type"]
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_put_text_empty_body_still_sets_content_type():
    """``put_xml(path, "")`` is used for reboot / recording toggles.

    An empty body must still send ``Content-Type: application/xml``,
    otherwise some firmwares reject the PUT with ``badXmlContent``.
    """
    snapshots: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        snapshots.append({
            "body": request.content,
            "content_type": request.headers.get("content-type", ""),
        })
        return httpx.Response(
            200,
            content=b"<ResponseStatus><statusCode>1</statusCode></ResponseStatus>",
            headers={"Content-Type": "application/xml"},
        )

    client = _client_with_transport(handler)
    try:
        await client.put_text("/ISAPI/System/reboot", "")
        assert len(snapshots) == 1
        assert snapshots[0]["body"] == b""
        assert "application/xml" in snapshots[0]["content_type"]
    finally:
        await client._client.aclose()
