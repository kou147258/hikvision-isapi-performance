"""Tests for v0.6.13 XML namespace stripping.

Pre-v0.6.13 the ISAPI client returned ``<DeviceInfo>`` style parsed
elements from Hikvision firmware directly. ElementTree prefixes
every tag with the default namespace URI, so plain-tag queries
like ``root.find("model")`` fail on real Hikvision responses:

    <DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">
        <model>DS-2CD2</model>
    </DeviceInfo>

ET parses ``<model>`` as ``{http://www.hikvision.com/ver20/XMLSchema}model``.
Without namespace-aware queries (e.g. ``root.find(".//{http://www.hikvision.com/ver20/XMLSchema}model")``),
the coordinator's plain-tag parsers get ``None`` for every field,
and every sensor shows "unknown" in HA even though the device is
responding correctly.

v0.6.13 strips ``xmlns="..."`` declarations from the response body
before ``ET.fromstring`` parses it. Plain-tag lookups then match
correctly. The strip is a regex over the raw bytes — no
XML-rewriting library needed, no risk of mangling attribute order
(which Hikvision is picky about for round-tripping).

This test file pins the strip behavior end-to-end via the public
``get_xml`` method, with ``httpx.MockTransport``.
"""

from __future__ import annotations

import httpx
import pytest

from custom_components.hikvision_isapi_performance.coordinator import (
    _parse_device_info,
    _parse_system_status,
)
from custom_components.hikvision_isapi_performance.isapi_client import (
    ISAPIClient,
    _strip_xmlns,
)


def _build_mock_client(handler) -> ISAPIClient:
    # The transport must be passed to the constructor — httpx binds it
    # inside __init__ and ignores later assignment to ``_transport``.
    # See test_v0612_httpx.py for the full explanation.
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


# ---- _strip_xmlns unit tests ----


def test_strip_xmlns_removes_default_namespace():
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">\n'
        '  <model>DS-2CD2</model>\n'
        '</DeviceInfo>'
    )
    out = _strip_xmlns(text)
    assert 'xmlns=' not in out
    assert "<model>" in out
    # Default-namespace stripped; element still parses with plain tags.
    from xml.etree import ElementTree as ET
    root = ET.fromstring(out)
    assert root.find("model").text == "DS-2CD2"


def test_strip_xmlns_removes_prefixed_namespace():
    text = (
        '<root xmlns:ns1="http://example.com/ns" '
        'xmlns:ns2="http://other.com/ns">'
        '<ns1:tag>hello</ns1:tag>'
        '</root>'
    )
    out = _strip_xmlns(text)
    assert 'xmlns' not in out
    # ns1 prefix is now dangling — but that's fine for our parser
    # since we don't use prefixed tags in our XML fixtures.


def test_strip_xmlns_handles_no_namespace():
    text = "<DeviceInfo><model>DS-2CD2</model></DeviceInfo>"
    out = _strip_xmlns(text)
    assert out == text


def test_strip_xmlns_handles_multiple_xmlns():
    text = (
        '<root xmlns:a="http://x" xmlns:b="http://y">'
        '<a:tag>1</a:tag><b:tag>2</b:tag>'
        '</root>'
    )
    out = _strip_xmlns(text)
    assert 'xmlns' not in out


def test_strip_xmlns_handles_single_quoted_xmlns():
    """Some firmwares use single quotes around namespace URIs."""
    text = "<root xmlns='http://x.com'><tag>hello</tag></root>"
    out = _strip_xmlns(text)
    assert 'xmlns' not in out


def test_strip_xmlns_preserves_other_attributes():
    """Version / type / other attrs are NOT namespace decls; they
    must survive the strip."""
    text = (
        '<DeviceInfo xmlns="http://x.com" version="2.0" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<model>DS-2CD2</model>'
        '</DeviceInfo>'
    )
    out = _strip_xmlns(text)
    assert 'version="2.0"' in out
    assert "<model>" in out
    assert "xmlns" not in out


# ---- end-to-end: get_xml on a namespaced response ----


@pytest.mark.asyncio
async def test_get_xml_parses_namespaced_response():
    """Real Hikvision shape with xmlns — get_xml must return a
    tree where plain tag finds work."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema" '
        'version="2.0">\n'
        '  <deviceName>Front Door</deviceName>\n'
        '  <model>DS-2CD2143G2-I</model>\n'
        '  <serialNumber>SN-9876</serialNumber>\n'
        '  <firmwareVersion>V5.7.10</firmwareVersion>\n'
        '  <deviceType>IPCamera</deviceType>\n'
        '  <macAddress>00:11:22:33:44:55</macAddress>\n'
        '</DeviceInfo>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
        )

    client = _build_mock_client(handler)
    try:
        root = await client.get_xml("/ISAPI/System/deviceInfo")
        # Coordinator's plain-tag parser must work.
        info = _parse_device_info(root)
        assert info["model"] == "DS-2CD2143G2-I"
        assert info["serialNumber"] == "SN-9876"
        assert info["firmwareVersion"] == "V5.7.10"
        assert info["deviceType"] == "IPCamera"
        assert info["macAddress"] == "00:11:22:33:44:55"
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_get_xml_nested_schema_parses_with_xmlns():
    """/ISAPI/System/status uses nested CPUList/MemoryList; the
    deep ``root.find(".//CPU")`` queries also need namespace
    resolution."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<DeviceStatus xmlns="http://www.hikvision.com/ver20/XMLSchema" '
        'version="2.0">\n'
        '  <deviceUpTime>92914</deviceUpTime>\n'
        '  <CPUList>\n'
        '    <CPU>\n'
        '      <cpuUtilization>16</cpuUtilization>\n'
        '    </CPU>\n'
        '  </CPUList>\n'
        '  <MemoryList>\n'
        '    <Memory>\n'
        '      <memoryUsage>99</memoryUsage>\n'
        '      <memoryAvailable>9696</memoryAvailable>\n'
        '    </Memory>\n'
        '  </MemoryList>\n'
        '  <totalRebootCount>40</totalRebootCount>\n'
        '</DeviceStatus>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
        )

    client = _build_mock_client(handler)
    try:
        root = await client.get_xml("/ISAPI/System/status")
        status = _parse_system_status(root)
        assert status["cpuUtilization"] == "16"
        assert status["memoryUsage"] == "99"
        assert status["uptime"] == "92914"
        assert status["rebootCount"] == "40"
    finally:
        await client._client.aclose()


# ---- regression: bare (no-namespace) XML still works ----


@pytest.mark.asyncio
async def test_get_xml_bare_xml_still_parses():
    """The strip is a no-op for XML without xmlns declarations —
    pre-existing fixtures and tests must keep working."""
    body = (
        '<DeviceInfo>'
        '  <model>DS-2CD2</model>'
        '  <serialNumber>SN-X</serialNumber>'
        '</DeviceInfo>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
        )

    client = _build_mock_client(handler)
    try:
        root = await client.get_xml("/path")
        info = _parse_device_info(root)
        assert info["model"] == "DS-2CD2"
        assert info["serialNumber"] == "SN-X"
    finally:
        await client._client.aclose()


# ---- manifest version ----


def test_v0613_manifest_version_at_or_beyond_0_6_13():
    """v0.6.13 anchor; later releases may bump further. We assert
    the manifest is at or beyond v0.6.13 so this test stays green
    across subsequent bug-fix releases."""
    import json
    from pathlib import Path
    manifest = json.loads(Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\manifest.json"
    ).read_text(encoding="utf-8"))
    parts = manifest["version"].split(".")
    assert parts[0] == "0"
    assert int(parts[1]) >= 6
    # If we're still on 0.6.x, third part must be >= 13.
    if int(parts[1]) == 6:
        assert int(parts[2]) >= 13
