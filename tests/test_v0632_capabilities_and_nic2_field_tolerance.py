"""Regression tests for v0.6.32.

User feedback after v0.6.31 reported three regressions with the
device still showing "unknown" / missing:

1. **``capability_video_input_channels`` sensor stays "unknown".**
   The capabilities endpoint returned 200 (or some other shape)
   on the user's device but the XML didn't contain the
   ``<VideoInputChannelNums>`` tag we assumed. Some firmwares
   use ``<videoInputChannelNums>`` (camelCase) or different
   names entirely (``<InputChannelNum>``, ``<ChannelNum>``).

   Fix: ``_parse_capabilities`` walks a list of candidate tag
   names per field and returns the first one that matches
   and parses as an int.

2. **NVR second NIC sensor still doesn't appear.**
   ``_fetch_network_interfaces`` only tried the canonical
   ``/ISAPI/System/Network/interfaces`` endpoint. Some V4
   firmware variants expose the interface list at lowercase
   paths (``/ISAPI/System/network/interfaces``) or
   ``/ISAPI/Networking/interfaces``. Without fallback, the
   response is empty → NIC 2 never registers.

   Fix: try multiple endpoints; return the first one that
   returns valid XML.

3. **NVR / camera storage / SD card sensors don't show.**
   This is partly physical (IPC has no HDD) but the parser
   already handles ``root is None`` gracefully. New tests pin
   the parser contract so a future regression to the parser
   is caught.

Tests:
- ``_parse_capabilities`` accepts PascalCase tag
- ``_parse_capabilities`` accepts camelCase tag
- ``_parse_capabilities`` accepts stripped-down tag names
  (ChannelNum / NICNum)
- ``_parse_capabilities`` returns None when no candidate matches
- ``_parse_capabilities`` skips candidates that match but parse
  as garbage (e.g. ``"<garbage>"``)
- ``_find_int_field`` walks candidates in declared order
- ``_parse_network_interfaces`` (via _fetch_network_interfaces)
  isn't tested directly (requires live HTTP), but
  ``_find_int_field`` is — pins the new helper.
"""

from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(r"C:\Users\43457\Desktop\hikvision-isapi")
_INTEGRATION_ROOT = (
    _REPO_ROOT / "custom_components" / "hikvision_isapi_performance"
)


def _parse_xml(xml: str):
    """Strip xmlns and parse XML, returning root Element."""
    import xml.etree.ElementTree as ET
    from custom_components.hikvision_isapi_performance.isapi_client import (
        _strip_xmlns,
    )
    return ET.fromstring(_strip_xmlns(xml))


def _load_module(name: str):
    sys.path.insert(0, str(_INTEGRATION_ROOT.parent))
    from custom_components.hikvision_isapi_performance import coordinator
    return coordinator


# ---------------------------------------------------------------------------
# _parse_capabilities field-name tolerance
# ---------------------------------------------------------------------------


def test_v0632_capabilities_pascal_case_video_input():
    """V5 firmware uses ``<VideoInputChannelNums>`` (PascalCase).

    The original v0.6.28 parser was hardcoded to this. It still
    works on V5 — verify it didn't regress.
    """
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<VideoInputChannelNums>8</VideoInputChannelNums>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 8, (
        f"v0.6.32: PascalCase VideoInputChannelNums must still "
        f"parse (got {result['video_input_channels']!r})."
    )


def test_v0632_capabilities_camel_case_video_input():
    """Some firmware uses ``<videoInputChannelNums>`` (camelCase).

    v0.6.32 candidate list: this is the second variant tried.
    """
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<videoInputChannelNums>16</videoInputChannelNums>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 16, (
        f"v0.6.32: camelCase videoInputChannelNums must parse "
        f"(got {result['video_input_channels']!r})."
    )


def test_v0632_capabilities_input_channel_num():
    """Some firmware uses just ``<InputChannelNum>``."""
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<InputChannelNum>4</InputChannelNum>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 4


def test_v0632_capabilities_channel_num_fallback():
    """As a last resort, ``<ChannelNum>``."""
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<ChannelNum>2</ChannelNum>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 2


def test_v0632_capabilities_no_match_returns_none():
    """If none of the candidate tags are present, field is None."""
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<!-- no video input channel count -->
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] is None


def test_v0632_capabilities_garbage_value_skipped():
    """A candidate tag with non-numeric text is skipped, not crashed.

    ``<InputChannelNum>unknown</InputChannelNum>`` — parse fails
    gracefully and we fall through to the next candidate.
    """
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<InputChannelNum>unknown</InputChannelNum>
<ChannelNum>3</ChannelNum>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 3, (
        "v0.6.32: garbage text in earlier candidate must be "
        "skipped; the helper must walk to the next candidate."
    )


def test_v0632_capabilities_empty_text_skipped():
    """``<VideoInputChannelNums></VideoInputChannelNums>`` — empty text.

    Empty text is treated the same as missing tag.
    """
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<VideoInputChannelNums></VideoInputChannelNums>
<ChannelNum>7</ChannelNum>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 7


def test_v0632_capabilities_first_match_wins():
    """When multiple candidates match, the first wins (declared order).

    The candidate list is ordered by prevalence:
    PascalCase V5 → camelCase → InputChannelNum → ChannelNum.
    PascalCase must win when both are present.
    """
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<VideoInputChannelNums>1</VideoInputChannelNums>
<ChannelNum>99</ChannelNum>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 1, (
        "v0.6.32: PascalCase tag must win over ChannelNum "
        "(PascalCase is the canonical V5 form)."
    )


# ---------------------------------------------------------------------------
# _find_int_field helper
# ---------------------------------------------------------------------------


def test_v0632_find_int_field_helper_walks_candidates():
    """``_find_int_field`` returns the first matching int."""
    coord = _load_module("coordinator")
    xml = _parse_xml("""<root>
<first></first>
<second>42</second>
<third>99</third>
</root>""")
    fn = coord._find_int_field
    assert fn(
        xml,
        candidates=(".//first", ".//second", ".//third"),
    ) == 42
    # First candidate matches → others are not consulted.
    assert fn(
        xml,
        candidates=(".//second", ".//third"),
    ) == 42
    # No candidate matches → None.
    assert fn(
        xml,
        candidates=(".//missing", ".//also_missing"),
    ) is None


def test_v0632_find_int_field_helper_skips_empty_and_garbage():
    """``_find_int_field`` skips empty/garbage values."""
    coord = _load_module("coordinator")
    xml = _parse_xml("""<root>
<empty></empty>
<garbage>not-a-number</garbage>
<ok>123</ok>
</root>""")
    fn = coord._find_int_field
    assert fn(
        xml,
        candidates=(".//empty", ".//garbage", ".//ok"),
    ) == 123
    assert fn(
        xml,
        candidates=(".//empty", ".//garbage"),
    ) is None


# ---------------------------------------------------------------------------
# _fetch_network_interfaces fallback list
# ---------------------------------------------------------------------------


def test_v0632_network_interfaces_uses_fallback_endpoints():
    """``_fetch_network_interfaces`` must try multiple endpoints.

    The v0.6.32 helper walks 4 endpoints before giving up:
    - /ISAPI/System/Network/interfaces (canonical V5)
    - /ISAPI/System/network/interfaces (lowercase n)
    - /ISAPI/Networking/interfaces (V4 variant)
    - /ISAPI/System/NetworkInterface (V4 single-word)

    Source-load the method body and verify all 4 endpoints are
    referenced — guards against accidental reverts.
    """
    coord = _load_module("coordinator")
    src = (
        _INTEGRATION_ROOT / "coordinator.py"
    ).read_text(encoding="utf-8-sig")
    body_start = src.index("async def _fetch_network_interfaces(")
    # Walk forward to the next def / class.
    body_lines = src[body_start:].splitlines()
    end = 1
    while end < len(body_lines):
        line = body_lines[end]
        if line.startswith("def ") or line.startswith("class ") or (
            line.startswith("async def ") and end > 1
        ):
            break
        end += 1
    body = "\n".join(body_lines[:end])
    expected = [
        "/ISAPI/System/Network/interfaces",
        "/ISAPI/System/network/interfaces",
        "/ISAPI/Networking/interfaces",
        "/ISAPI/System/NetworkInterface",
    ]
    for endpoint in expected:
        assert endpoint in body, (
            f"v0.6.32: _fetch_network_interfaces must include "
            f"fallback endpoint {endpoint!r} (not found in "
            f"method body — was the fallback list reverted?)"
        )