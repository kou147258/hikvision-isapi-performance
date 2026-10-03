"""v0.8 步骤5：客户端 POST XML 能力（录像检索的前提）。

`POST /ISAPI/ContentMgmt/search` 是录像状态唯一的真实数据源，但
isapi_client 只有 get_* 与 put_*，缺 POST。本步补 post_text / post_xml。

契约：
  * post_xml(path, body) 发 POST，Content-Type=application/xml，
    响应去命名空间后解析为 ET.Element
  * 复用 _request 的认证回退（176.18 只接受 Basic）与错误映射
    （403→ISAPIAuthError, 400→ISAPIError）
  * 录像检索在部分设备返回 403/400，调用方靠这些异常优雅降级

全部用 httpx.MockTransport，零真实网络。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance.isapi_client import (  # noqa: E402
    ISAPIClient,
    ISAPIAuthError,
    ISAPIError,
)

SEARCH_PATH = "/ISAPI/ContentMgmt/search"

# 真机 CMSearchResult 响应（来自 fixtures_v08/10_18_176_64__L_track__101）
SEARCH_OK = """<?xml version="1.0" encoding="UTF-8" ?>
<CMSearchResult version="2.0">
<searchID>{81c7052c-fade-4fa9-b366-7c0e61aa8e30}</searchID>
<responseStatus>true</responseStatus>
<responseStatusStrg>OK</responseStatusStrg>
<numOfMatches>2</numOfMatches>
<matchList>
<searchMatchItem>
<trackID>101</trackID>
<timeSpan><startTime>2026-09-27T00:12:02Z</startTime>
<endTime>2026-09-27T00:29:38Z</endTime></timeSpan>
</searchMatchItem>
</matchList>
</CMSearchResult>"""

SEARCH_NO_MATCH = """<?xml version="1.0" encoding="UTF-8" ?>
<CMSearchResult version="2.0">
<responseStatus>true</responseStatus>
<responseStatusStrg>NO MATCHES</responseStatusStrg>
<numOfMatches>0</numOfMatches>
</CMSearchResult>"""


def _build_client(handler) -> ISAPIClient:
    client = ISAPIClient(
        host="10.18.176.64", username="admin", password="pass",
        port=80, use_https=False,
    )
    client._client = httpx.AsyncClient(
        timeout=10.0, verify=False, follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    return client


def _xml_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
    )


@pytest.mark.asyncio
async def test_post_xml_sends_post_with_body_and_content_type():
    """post_xml 发 POST 方法、携带请求体、设 Content-Type。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content.decode("utf-8")
        seen["ct"] = request.headers.get("content-type", "")
        if "authorization" not in {k.lower() for k in request.headers}:
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="n", qop="auth"'}
            )
        return _xml_response(SEARCH_OK)

    client = _build_client(handler)
    try:
        root = await client.post_xml(SEARCH_PATH, "<CMSearchDescription/>")
        assert seen["method"] == "POST"
        assert "<CMSearchDescription/>" in seen["body"]
        assert "application/xml" in seen["ct"].lower()
        assert root.tag.endswith("CMSearchResult")
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_post_xml_parses_namespaced_response():
    """响应带命名空间也能解析（去 xmlns）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {k.lower() for k in request.headers}:
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="n", qop="auth"'}
            )
        ns = SEARCH_OK.replace(
            "<CMSearchResult version=\"2.0\">",
            '<CMSearchResult version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
        )
        return _xml_response(ns)

    client = _build_client(handler)
    try:
        root = await client.post_xml(SEARCH_PATH, "<q/>")
        assert root.find(".//trackID").text == "101"
        assert root.find(".//responseStatusStrg").text == "OK"
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_post_xml_no_match_parses():
    """NO MATCHES 也是合法 200 响应，能解析（不抛异常）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {k.lower() for k in request.headers}:
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="n", qop="auth"'}
            )
        return _xml_response(SEARCH_NO_MATCH)

    client = _build_client(handler)
    try:
        root = await client.post_xml(SEARCH_PATH, "<q/>")
        assert root.find(".//responseStatusStrg").text == "NO MATCHES"
        assert root.find(".//searchMatchItem") is None
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_post_xml_403_raises_auth_error():
    """403（176.51/52/53 录像检索拒绝）→ ISAPIAuthError，供调用方降级。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return _xml_response("<ResponseStatus><statusCode>4</statusCode></ResponseStatus>",
                             status=403)

    client = _build_client(handler)
    try:
        with pytest.raises(ISAPIAuthError):
            await client.post_xml(SEARCH_PATH, "<q/>")
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_post_xml_400_raises_isapi_error():
    """400 Invalid track id（越界 trackID）→ ISAPIError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {k.lower() for k in request.headers}:
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="n", qop="auth"'}
            )
        return _xml_response(
            "<ResponseStatus><statusCode>4</statusCode>"
            "<subStatusCode>badXmlContent</subStatusCode></ResponseStatus>",
            status=400)

    client = _build_client(handler)
    try:
        with pytest.raises(ISAPIError):
            await client.post_xml(SEARCH_PATH, "<q/>")
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_post_xml_falls_back_to_basic_auth():
    """176.18 场景：Digest 401 且挑战头不含 digest → 回退 Basic。"""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        authz = request.headers.get("authorization", "")
        attempts.append(authz.split(" ")[0] if authz else "none")
        if not authz:
            # 挑战头是 Basic（不含 digest），触发客户端 Basic 回退
            return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="DS-FB2127"'})
        if authz.startswith("Basic"):
            return _xml_response(SEARCH_OK)
        return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="DS-FB2127"'})

    client = _build_client(handler)
    try:
        root = await client.post_xml(SEARCH_PATH, "<q/>")
        assert root.tag.endswith("CMSearchResult")
        assert "Basic" in attempts
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_post_text_returns_raw_body():
    """post_text 返回原始文本（不解析）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {k.lower() for k in request.headers}:
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="n", qop="auth"'}
            )
        return _xml_response(SEARCH_OK)

    client = _build_client(handler)
    try:
        text = await client.post_text(SEARCH_PATH, "<q/>")
        assert "CMSearchResult" in text
        assert isinstance(text, str)
    finally:
        await client._client.aclose()
