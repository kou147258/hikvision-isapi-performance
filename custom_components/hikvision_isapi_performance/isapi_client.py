"""Async ISAPI client for Hikvision V5.x firmware + V4 NVR mixed fleet.

v0.6.12: migrated from aiohttp + hand-rolled Digest auth to httpx with
built-in ``DigestAuth`` / ``BasicAuth`` classes. Root cause of the
v0.6.11 "all entities unavailable" issue:

- The hand-rolled Digest computation doesn't handle all the algorithm
  variants Hikvision firmware ships (MD5-sess, SHA-256 on newer V5.7+,
  etc.) — aiohttp 3.8+ removed its built-in ``DigestAuth`` class so
  we had to re-implement it, but our implementation only supports the
  MD5 + ``qop=auth`` path. On V5.7+ firmware that advertises
  ``algorithm=SHA-256`` in the challenge, the digest response is
  computed wrong → server returns 401 → coordinator marks the device
  unavailable with the misleading "Authentication failed" message
  in logs.
- The per-channel loop's except tuple was ``(ISAPIError,
  ISAPIAuthError)`` but NOT ``ISAPIConnectionError`` — a transient
  network blip on one channel would raise unhandled, propagate out
  of ``_async_update_data``, and HA would mark ALL entities for the
  device unavailable. Coordinators MUST catch all I/O exception
  families and convert them to ``UpdateFailed``, not let them
  propagate.

httpx's built-in ``DigestAuth`` (anyio-based, battle-tested in the
production hikvision_isapi integration) handles every auth variant
Hikvision firmware ships. The smart-fallback pattern that lets the
client gracefully downgrade from digest to basic-on-401 matches the
``curl --anyauth`` behavior.

The auth-flow logic:

1. Send with ``auth=self._auth`` (initially DigestAuth)
2. If 401 AND WWW-Authenticate does NOT contain "digest" anywhere
   (e.g. old DS-2CD8464F-EI returns ``WWW-Authenticate: Basic
   realm="IPCamera"``), retry the same request with BasicAuth. If
   the basic response is non-401, switch ``self._auth`` to BasicAuth
   for the remainder of the session.
3. If 401 AND WWW-Authenticate DOES contain "digest", the credentials
   are wrong — DON'T retry with basic, that would burn another
   failed login toward Hikvision's account lockout.

This matches ``curl --anyauth`` semantics with one enhancement: the
auth switch is sticky per-client (saves the per-request handshake on
subsequent calls once we've confirmed which scheme the device uses).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from xml.etree import ElementTree as ET

import httpx

_LOGGER = logging.getLogger(__name__)


class ISAPIAuthError(Exception):
    """Raised when the device rejects our credentials (HTTP 401/403).

    Hikvision returns 401 for wrong credentials and 403 for valid
    credentials but insufficient privileges on the target endpoint.
    The user message in our exceptions explains both cases so the HA
    log makes the cause clear.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ISAPIConnectionError(Exception):
    """Raised on network / TLS / timeout failures.

    Treated by the coordinator as ``UpdateFailed`` so a single
    transient blip doesn't permanently mark the device unavailable.
    """


class ISAPIError(Exception):
    """Generic ISAPI failure (HTTP 4xx/5xx other than auth, parse errors)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# v0.6.13: Hikvision V5.x firmware returns XML with a default
# namespace declaration like
#   ``<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">``
# on every response. Python's ElementTree then prefixes every tag
# with that namespace (``{http://...}model``), which means a plain
# ``root.find("model")`` returns None. The coordinator's XML
# parsers (``_parse_device_info``, ``_parse_system_status``, …)
# all use plain-tag lookups, so without this fix they all return
# empty defaults and every sensor shows "unknown" in HA even
# though the device is responding.
#
# We strip ``xmlns`` declarations before parsing so plain tag
# matching works on Hikvision's namespaced responses. We preserve
# other attributes (``version="2.0"`` etc.) by only removing the
# attribute declaration itself, not the rest of the tag.
_XMLNS_RE = re.compile(
    # Matches ``xmlns="..."`` and ``xmlns:foo="..."``. Whitespace-
    # tolerant because Hikvision uses inconsistent spacing between
    # the attribute name and the value.
    r"""\s+xmlns(:[a-zA-Z][a-zA-Z0-9_-]*)?\s*=\s*"[^"]*"|\s+xmlns(:[a-zA-Z][a-zA-Z0-9_-]*)?\s*=\s*'[^']*'"""
)


def _strip_xmlns(text: str) -> str:
    """Remove XML namespace declarations from a response body.

    Hikvision responses occasionally also use repeated xmlns on
    child elements (``:xsi:`` style prefixes). We strip them all so
    ElementTree can match plain tag names.
    """
    return _XMLNS_RE.sub("", text)


def _extract_challenge_lower(headers: httpx.Headers) -> str:
    """Return the WWW-Authenticate header, lowercased, stripped."""
    return (headers.get("WWW-Authenticate", "") or "").strip().lower()


def _validate_credentials_encodable(username: str, password: str) -> None:
    """Raise ``ISAPIAuthError`` if either credential can't encode to UTF-8.

    v0.7.1: restores the v0.6.8 guard lost in the v0.6.12 httpx
    migration. httpx encodes credentials in ``DigestAuth.__init__`` /
    ``BasicAuth.__init__``, so a lone surrogate (``"\\ud800"``) raises a
    bare ``UnicodeEncodeError`` during ISAPIClient construction — long
    before any request, and with no context about which credential was
    at fault.

    We name only the offending field, never its value: a password would
    otherwise land verbatim in the HA log file.
    """
    for field, value in (("username", username), ("password", password)):
        if not isinstance(value, str):
            continue
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ISAPIAuthError(
                f"ISAPI {field} contains characters that cannot be "
                f"encoded as UTF-8 ({exc.reason}). Re-enter the "
                f"credential using only characters supported by the "
                f"device."
            ) from exc


class ISAPIClient:
    """Async ISAPI client.

    Use as an async context manager::

        async with ISAPIClient(host, username, password) as client:
            info = await client.get_xml("/ISAPI/System/deviceInfo")

    The client owns a single ``httpx.AsyncClient`` for the lifetime of
    the ``async with`` block. Connections are reused across requests
    (no per-call TCP handshake); the auth class is per-request so it
    can switch from Digest to Basic mid-session if the server doesn't
    offer Digest.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 80,
        verify_ssl: bool = False,
        use_https: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._use_https = use_https
        self._timeout = timeout
        # v0.7.1: validate credentials are UTF-8 encodable BEFORE handing
        # them to httpx. ``httpx.DigestAuth.__init__`` calls
        # ``to_bytes(password)`` immediately, which raises a bare
        # ``UnicodeEncodeError`` for lone surrogates (e.g. a password
        # mangled by a paste or a corrupted config entry).
        #
        # v0.6.8 guarded this with ``_validate_credentials_encoding`` but
        # the guard was dropped during the v0.6.12 aiohttp→httpx
        # migration, and the failure moved EARLIER — from request time to
        # construction time — so every entity for the entry died with an
        # inscrutable codec traceback in the HA log.
        #
        # Raising ISAPIAuthError here is handled by both call sites:
        #   - config_flow: surfaces as the "invalid_auth" form error
        #   - coordinator: _make_client() runs inside the refresh try
        #     block, which converts ISAPIAuthError to UpdateFailed
        _validate_credentials_encodable(username, password)
        # Two built-in auth classes from httpx. ``DigestAuth`` handles
        # every algorithm variant Hikvision ships (MD5, SHA-256,
        # MD5-sess); ``BasicAuth`` is for the small subset of old
        # firmware that only supports Basic.
        self._digest_auth = httpx.DigestAuth(username, password)
        self._basic_auth = httpx.BasicAuth(username, password)
        # We start with Digest. If the server's first 401 doesn't
        # offer digest, we switch to Basic and never switch back.
        self._auth = self._digest_auth
        self._client: httpx.AsyncClient | None = None
        scheme = "https" if use_https else "http"
        self._base_url = f"{scheme}://{host}:{port}"

    async def __aenter__(self) -> "ISAPIClient":
        # ``verify=False`` accepts self-signed certs; the alternative
        # is to add a CA bundle path but that's per-deployment
        # configuration we don't have access to.
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            verify=self._verify_ssl,
            follow_redirects=True,
        )
        _LOGGER.debug(
            "ISAPIClient opened: %s (use_https=%s, verify_ssl=%s)",
            self._base_url, self._use_https, self._verify_ssl,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def host(self) -> str:
        return self._host

    async def _request(self, method: str, path: str, **kwargs: Any) -> str:
        """Issue an HTTP request with the current auth class.

        Implements the smart-fallback pattern described in the module
        docstring. Returns the response body as text.
        """
        if self._client is None:
            raise ISAPIConnectionError("Client not opened")
        url = f"{self._base_url}{path}"
        try:
            resp = await self._client.request(
                method, url, auth=self._auth, **kwargs
            )
        except (
            httpx.ConnectError, httpx.ConnectTimeout,
            httpx.ReadTimeout, httpx.WriteTimeout,
            httpx.PoolTimeout, httpx.NetworkError,
            httpx.TimeoutException, httpx.RemoteProtocolError,
        ) as exc:
            raise ISAPIConnectionError(
                f"network error talking to {self._host}: {exc}"
            ) from exc

        if (
            resp.status_code == 401
            and self._auth is not self._basic_auth
        ):
            # First 401 — try Basic only if the server's challenge
            # doesn't offer Digest. (If Digest IS offered, the
            # credentials are wrong — retrying with Basic would just
            # burn another failed login toward the device's
            # account-lockout policy.)
            challenge = _extract_challenge_lower(resp.headers)
            if "digest" not in challenge:
                _LOGGER.debug(
                    "%s: 401 with non-digest challenge %r; retrying "
                    "with basic auth",
                    self._host, challenge[:120],
                )
                try:
                    basic_resp = await self._client.request(
                        method, url, auth=self._basic_auth, **kwargs
                    )
                except (
                    httpx.ConnectError, httpx.ConnectTimeout,
                    httpx.ReadTimeout, httpx.WriteTimeout,
                    httpx.PoolTimeout, httpx.NetworkError,
                    httpx.TimeoutException, httpx.RemoteProtocolError,
                ) as exc:
                    raise ISAPIConnectionError(
                        f"network error talking to {self._host} "
                        f"(basic auth retry): {exc}"
                    ) from exc
                if basic_resp.status_code != 401:
                    _LOGGER.info(
                        "%s: switched to basic auth (digest not "
                        "supported by firmware)",
                        self._host,
                    )
                    self._auth = self._basic_auth
                    resp = basic_resp

        if resp.status_code in (401, 403):
            body = (resp.text or "")[:300]
            raise ISAPIAuthError(
                f"HTTP {resp.status_code} on {url}: {body}",
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            body = (resp.text or "")[:300]
            raise ISAPIError(
                f"HTTP {resp.status_code} on {url}: {body}",
                status_code=resp.status_code,
            )
        return resp.text

    async def _request_bytes(self, method: str, path: str, **kwargs: Any) -> bytes:
        """Like ``_request`` but returns the raw response body."""
        if self._client is None:
            raise ISAPIConnectionError("Client not opened")
        url = f"{self._base_url}{path}"
        try:
            resp = await self._client.request(
                method, url, auth=self._auth, **kwargs
            )
        except (
            httpx.ConnectError, httpx.ConnectTimeout,
            httpx.ReadTimeout, httpx.WriteTimeout,
            httpx.PoolTimeout, httpx.NetworkError,
            httpx.TimeoutException, httpx.RemoteProtocolError,
        ) as exc:
            raise ISAPIConnectionError(
                f"network error talking to {self._host}: {exc}"
            ) from exc

        if (
            resp.status_code == 401
            and self._auth is not self._basic_auth
        ):
            challenge = _extract_challenge_lower(resp.headers)
            if "digest" not in challenge:
                try:
                    basic_resp = await self._client.request(
                        method, url, auth=self._basic_auth, **kwargs
                    )
                except (
                    httpx.ConnectError, httpx.ConnectTimeout,
                    httpx.ReadTimeout, httpx.WriteTimeout,
                    httpx.PoolTimeout, httpx.NetworkError,
                    httpx.TimeoutException, httpx.RemoteProtocolError,
                ) as exc:
                    raise ISAPIConnectionError(
                        f"network error talking to {self._host} "
                        f"(basic auth retry): {exc}"
                    ) from exc
                if basic_resp.status_code != 401:
                    self._auth = self._basic_auth
                    resp = basic_resp

        if resp.status_code in (401, 403):
            raise ISAPIAuthError(
                f"HTTP {resp.status_code} on {url}",
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            body = (resp.text or "")[:300]
            raise ISAPIError(
                f"HTTP {resp.status_code} on {url}: {body}",
                status_code=resp.status_code,
            )
        return resp.content

    # ---- Public API ----

    async def get_text(self, path: str) -> str:
        """GET ``path`` and return the response body as text."""
        return await self._request("GET", path)

    async def get_bytes(self, path: str) -> bytes:
        """GET ``path`` and return the raw response body."""
        return await self._request_bytes("GET", path)

    async def get_xml(self, path: str) -> ET.Element:
        """GET ``path`` and parse the response as XML."""
        text = await self.get_text(path)
        # v0.6.13: Hikvision's responses use a default namespace;
        # strip it before parsing so ``root.find("model")`` etc.
        # match plain tag names instead of the namespaced form.
        text = _strip_xmlns(text)
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise ISAPIError(f"invalid XML from {path}: {exc}") from exc

    async def put_text(self, path: str, body: str) -> str:
        """PUT ``body`` (XML) to ``path`` and return the response body."""
        return await self._request(
            "PUT",
            path,
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/xml; charset=UTF-8"},
        )

    async def put_xml(self, path: str, body: str) -> ET.Element:
        """PUT ``body`` (XML) to ``path`` and parse the response."""
        text = await self.put_text(path, body)
        # v0.6.13: same namespace-strip as ``get_xml`` (Hikvision
        # PUT responses are namespaced too).
        text = _strip_xmlns(text)
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise ISAPIError(f"invalid XML from {path}: {exc}") from exc
