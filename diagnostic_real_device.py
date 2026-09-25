"""Real-device diagnostic for hikvision_isapi_performance.

Standalone script — no HA, no conftest, no pytest. Just `requests`.

Two modes:

MODE 1 (live device, default): connect to a real Hikvision device
and exercise the coordinator's parsing pipeline. Useful when
sensors show "unknown" — answers "what does my device actually
return vs what the integration expects to parse".

    python diagnostic_real_device.py --host 10.18.176.65 --user admin --password xxx
    python diagnostic_real_device.py --host 10.18.176.65 --user admin --password xxx --https

MODE 2 (paste XML): paste a captured XML response from your
device and run the parser against it. Useful when you already
have the raw XML from a browser/curl/wireshark.

    python diagnostic_real_device.py --paste --endpoint capabilities --xml-file cap.xml
    python diagnostic_real_device.py --paste --endpoint network --xml-file net.xml
    python diagnostic_real_device.py --paste --endpoint storage --xml-file stor.xml

Output: a JSON dict showing exactly what the integration would
store, plus a list of "concerns" (e.g. "expected <VideoInputChannelNums>
but found nothing matching any candidate tag").
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib3
import xml.etree.ElementTree as ET
from typing import Any
from xml.etree import ElementTree as ET

# Match `xmlns="..."`, `xmlns:hi="..."` (double- AND single-quoted).
# Mirrors ``isapi_client._strip_xmlns`` so the diagnostic parser sees
# exactly the same tag names as the production coordinator.
_XMLNS_RE = re.compile(
    r"""\s+xmlns(:[a-zA-Z][a-zA-Z0-9_-]*)?\s*=\s*"[^"]*"|\s+xmlns(:[a-zA-Z][a-zA-Z0-9_-]*)?\s*=\s*'[^']*'""",
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Try `requests` first; fall back to stdlib `urllib.request` +
# hand-rolled HTTP Digest auth (Hikvision's only auth scheme).
try:
    import requests
    from requests.auth import HTTPDigestAuth
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False
    # stdlib fallback uses urllib + hashlib for Digest auth.
    import hashlib
    import urllib.request
    import urllib.error


# ---------------------------------------------------------------------------
# ISAPI client (lightweight, no HA dependency)
# ---------------------------------------------------------------------------


class IsapiFetchError(Exception):
    pass


def fetch_isapi(
    host: str,
    user: str,
    password: str,
    endpoint: str,
    *,
    use_https: bool = False,
    timeout: float = 8.0,
) -> str:
    """GET one ISAPI endpoint, return raw XML text."""
    if _HAS_REQUESTS:
        return _fetch_isapi_requests(
            host, user, password, endpoint,
            use_https=use_https, timeout=timeout,
        )
    return _fetch_isapi_stdlib(
        host, user, password, endpoint,
        use_https=use_https, timeout=timeout,
    )


def _fetch_isapi_requests(
    host: str,
    user: str,
    password: str,
    endpoint: str,
    *,
    use_https: bool,
    timeout: float,
) -> str:
    """Probe one ISAPI endpoint with Digest→Basic auth fallback.

    Mirrors the HA integration's ``isapi_client.py`` auth flow:
    first try Digest (the standard Hikvision scheme). If 401
    AND the server's WWW-Authenticate header does NOT contain
    "digest" (i.e. an old firmware variant that only offers
    Basic), retry with BasicAuth. Sticky per call — we don't
    mutate global state, just retry this single request.

    If the server DOES offer digest but our creds are wrong,
    the second 401 is the real error — surface it.
    """
    scheme = "https" if use_https else "http"
    url = f"{scheme}://{host}{endpoint}"

    # 1. Try Digest.
    try:
        resp = requests.get(
            url,
            auth=HTTPDigestAuth(user, password),
            timeout=timeout,
            verify=False,
        )
    except requests.exceptions.Timeout as exc:
        raise IsapiFetchError(f"timeout after {timeout}s") from exc
    except requests.exceptions.ConnectionError as exc:
        raise IsapiFetchError(f"connection error: {exc}") from exc
    except Exception as exc:
        raise IsapiFetchError(f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code != 401:
        if resp.status_code == 404:
            raise IsapiFetchError(
                "HTTP 404 — endpoint not present on this device"
            )
        if resp.status_code >= 400:
            snippet = resp.text[:300].replace("\n", " ")
            raise IsapiFetchError(
                f"HTTP {resp.status_code} — body: {snippet!r}"
            )
        return resp.text

    # 2. Got 401. Inspect WWW-Authenticate. Some Hikvision firmwares
    # (DS-7708 V4.1.x confirmed in the user's fleet) advertise BOTH
    # Digest and Basic. curl --anyauth picks Basic first; requests
    # DigestAuth sends Digest. Try Basic as a fallback to disambiguate
    # "creds wrong" vs "this firmware variant prefers Basic".
    www_auth_raw = resp.headers.get("WWW-Authenticate", "") or ""
    www_auth = www_auth_raw.lower()

    # Always try Basic if the challenge advertises it. Some firmwares
    # accept Basic even when Digest is also offered (curl --anyauth).
    if "basic" in www_auth:
        try:
            resp2 = requests.get(
                url,
                auth=requests.auth.HTTPBasicAuth(user, password),
                timeout=timeout,
                verify=False,
            )
        except requests.exceptions.Timeout as exc:
            raise IsapiFetchError(f"timeout after {timeout}s") from exc
        except requests.exceptions.ConnectionError as exc:
            raise IsapiFetchError(f"connection error: {exc}") from exc
        if resp2.status_code != 401:
            if resp2.status_code == 404:
                raise IsapiFetchError(
                    "HTTP 404 — endpoint not present on this device"
                )
            if resp2.status_code >= 400:
                snippet = resp2.text[:300].replace("\n", " ")
                raise IsapiFetchError(
                    f"HTTP {resp2.status_code} — body: {snippet!r}"
                )
            print(
                f"  (Basic auth succeeded where Digest failed; "
                f"this firmware prefers Basic)"
            )
            return resp2.text
        # Basic also failed — creds wrong either way.
        raise IsapiFetchError(
            f"HTTP 401 with both Digest AND Basic — creds wrong. "
            f"WWW-Authenticate: {www_auth_raw!r}"
        )

    # Only Digest was advertised and it failed.
    raise IsapiFetchError(
        f"HTTP 401 with Digest challenge — creds wrong. "
        f"WWW-Authenticate: {www_auth_raw!r}"
    )


def _fetch_isapi_stdlib(
    host: str,
    user: str,
    password: str,
    endpoint: str,
    *,
    use_https: bool,
    timeout: float,
) -> str:
    """stdlib fallback: urllib + hand-rolled Digest auth.

    Hikvision uses HTTP Digest. urllib's basic auth handler does
    not support Digest, so we do the 401 → nonce → re-request
    dance ourselves.
    """
    import os
    import random
    import urllib.parse

    scheme = "https" if use_https else "http"
    url = f"{scheme}://{host}{endpoint}"

    # First unauthenticated request to get the challenge.
    try:
        req = urllib.request.Request(url)
        ctx = (
            urllib.request.ssl._create_unverified_context()
            if use_https else None
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code != 401:
                snippet = e.read().decode("utf-8", errors="replace")[:300]
                raise IsapiFetchError(
                    f"HTTP {e.code} — body: {snippet!r}"
                ) from e
            challenge = e.headers.get("WWW-Authenticate", "")
            if not challenge.lower().startswith("digest"):
                raise IsapiFetchError(
                    f"HTTP 401 with non-Digest challenge: {challenge!r}"
                ) from e
            www_auth = e.headers.get("WWW-Authenticate")
    except urllib.error.URLError as exc:
        raise IsapiFetchError(f"{type(exc).__name__}: {exc}") from exc

    # Parse Digest challenge.
    challenge = {}
    for part in www_auth.split(",")[1:]:  # skip "Digest"
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        challenge[k.strip()] = v.strip().strip('"')

    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "")
    opaque = challenge.get("opaque", "")

    # Compute Digest response.
    nc_value = "00000001"
    cnonce = hashlib.md5(
        os.urandom(8)
    ).hexdigest()
    method = "GET"
    uri = endpoint  # request URI = path portion
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop:
        qop_dir = qop.split(",")[0].strip()
        response_digest = hashlib.md5(
            f"{ha1}:{nonce}:{nc_value}:{cnonce}:{qop_dir}:{ha2}".encode()
        ).hexdigest()
    else:
        response_digest = hashlib.md5(
            f"{ha1}:{nonce}:{ha2}".encode()
        ).hexdigest()

    auth_str = (
        f'Digest username="{user}", realm="{realm}", '
        f'nonce="{nonce}", uri="{uri}", '
        f'response="{response_digest}"'
    )
    if qop:
        auth_str += f', qop={qop.split(",")[0].strip()}, nc={nc_value}, cnonce="{cnonce}"'
    if opaque:
        auth_str += f', opaque="{opaque}"'

    # Retry with Authorization header.
    req = urllib.request.Request(url)
    req.add_header("Authorization", auth_str)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        snippet = e.read().decode("utf-8", errors="replace")[:300]
        raise IsapiFetchError(
            f"HTTP {e.code} — body: {snippet!r}"
        ) from e


def _hex_dump(text: str, max_bytes: int = 120) -> str:
    """Return a hex+repr view of the first ``max_bytes`` bytes.

    Used in error messages so the user can see exactly what's at
    the start of a misbehaving XML response (BOM, leading whitespace,
    PHP warning, etc.). Non-printable bytes are shown as ``\\xNN``;
    printable ASCII is shown as-is. UTF-8 multi-byte sequences appear
    as their raw bytes — by design.
    """
    raw = text.encode("utf-8", errors="replace")[:max_bytes]
    parts: list[str] = []
    printable: list[str] = []
    for b in raw:
        if 0x20 <= b < 0x7F:
            printable.append(chr(b))
        elif b in (0x09, 0x0A, 0x0D):
            printable.append(chr(b))
        else:
            printable.append(".")
        parts.append(f"{b:02x}")
    return (
        f"len={len(text.encode('utf-8', errors='replace'))} bytes | "
        f"{' '.join(parts)} | {''.join(printable)!r}"
    )


def _strip_xmlns(xml: str) -> ET.Element:
    """Strip default xmlns so we can find tags by simple name.

    Mirrors ``isapi_client._strip_xmlns``: drop xmlns attribute
    declarations via regex, then parse. No wrapper element.

    Real-device responses are messy. We defensively handle:

      * UTF-8 BOM (``\\xef\\xbb\\xbf``) at the very start
      * leading whitespace / blank lines before ``<?xml``
      * stray XML comments / processing instructions
      * the ``<?xml version="1.0" ...?>`` declaration itself
        (ElementTree may reject it after stripping); since
        ``fromstring`` ignores XML decls when they precede the
        root element we keep it but normalise surrounding junk.

    On any failure, raises ``ET.ParseError`` with the first 120 bytes
    of the offending text appended (so the caller can show the hex
    dump) — but parsing itself does NOT swallow real errors like
    unmatched tags.
    """
    text = xml
    # 1. UTF-8 BOM if present.
    if text.startswith("\ufeff"):
        text = text[1:]
    # 2. Drop XML comments / processing instructions (rare on ISAPI
    # but harmless). ``?xml`` is left in place — ET handles a
    # leading `<?xml ?>` declaration correctly.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # 3. Some firmwares interleave comments with whitespace; leading
    # whitespace is fine for ET, but a stray BOM after another BOM
    # (or a UTF-16 LE BOM ``\xff\xfe`` parsed as UTF-8 from the
    # terminal) would not be. Only lstrip one-shot.
    text = text.lstrip()
    # 4. If by now the text still doesn't start with '<' or '<?',
    # bin everything before the first '<' (up to 256 junk bytes).
    if text and text[0] not in "<":
        idx = text.find("<")
        if 0 < idx <= 256:
            text = text[idx:]
    # 5. Strip xmlns declarations.
    text = _XMLNS_RE.sub("", text)
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        # Bubble the same exception the production code raises, but
        # attach the head bytes so the caller can surface them.
        raise ET.ParseError(
            f"{exc}\n  first bytes: {_hex_dump(text)}"
        ) from exc


# ---------------------------------------------------------------------------
# Parsers (mirroring coordinator.py logic — keep these in sync if
# coordinator.py changes)
# ---------------------------------------------------------------------------


def _xml_text(element, *path: str) -> str | None:
    if element is None:
        return None
    for tag in path:
        element = element.find(tag)
        if element is None:
            return None
    return element.text


def _safe_int_mb(value) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _find_int_field(root: ET.Element, candidates: tuple[str, ...]) -> int | None:
    for path in candidates:
        node = root.find(path)
        if node is not None and node.text:
            value = _safe_int_mb(node.text)
            if value is not None:
                return value
    return None


def parse_capabilities(root: ET.Element | None) -> dict[str, Any]:
    if root is None:
        return {}
    out: dict[str, Any] = {}
    sys_status = root.find(".//SysStatus")
    if sys_status is not None:
        supported_attr = sys_status.attrib.get("supported")
        if supported_attr is not None:
            out["status_supported"] = supported_attr.strip().lower() == "true"
        else:
            out["status_supported"] = True
    else:
        out["status_supported"] = None

    out["video_input_channels"] = _find_int_field(
        root,
        candidates=(
            ".//VideoCap/videoInputPortNums",
            ".//videoInputPortNums",
            ".//VideoInputPortNums",
            ".//VideoInputChannelNums",
            ".//videoInputChannelNums",
            ".//InputChannelNum",
            ".//ChannelNum",
        ),
    )
    out["ethernet_interfaces"] = _find_int_field(
        root,
        candidates=(
            ".//EthernetNums",
            ".//ethernetNums",
            ".//NICNum",
            ".//NetworkInterfaceNum",
        ),
    )
    types: list[str] = []
    for dt in root.findall(".//SupportDeviceType/DeviceType"):
        if dt.text:
            types.append(dt.text.strip())
    out["device_types"] = types
    return out


def parse_network_interfaces(root: ET.Element | None) -> list[dict[str, Any]]:
    """Parse /ISAPI/System/Network/interfaces.

    Returns a list of interface dicts. Empty list if root is None.

    Hikvision firmware uses several XML shapes:
    - V5: <NetworkInterfaceList><NetworkInterface>...
    - V4: <NetworkInterfaces><NetworkInterface>...
    - Some: root itself is the list-like element (its direct
      children are <NetworkInterface> entries).

    Within a <NetworkInterface>, IP fields follow one of two shapes:

    V5 NVR / V5 IPC (the user's 10.18.176.10)::

        <NetworkInterface>
          <IPAddress>  (text-direct)
            <ipVersion>dual</ipVersion>
            <ipAddress>10.18.176.10</ipAddress>   <-- real IP
            <subnetMask>...</subnetMask>
            <DefaultGateway>
              <ipAddress>10.18.176.1</ipAddress>   <-- nested gateway
            </DefaultGateway>
          </IPAddress>
          ...
        </NetworkInterface>

    Or V5 alternate (text-direct IPAddress element)::

        <NetworkInterface>
          <IPAddress>192.168.1.10</IPAddress>
          <subnetMask>...</subnetMask>
          <DefaultGateway>192.168.1.1</DefaultGateway>
        </NetworkInterface>

    ``_ip_field`` tries the nested form first, then the direct text.

    We try each LIST shape in turn. The root element may already
    BE the list (post-_strip_xmlns lifts the first child), so we
    check the root's tag too.
    """
    if root is None:
        return []

    def _ip_field(iface: ET.Element, tag: str) -> str | None:
        """Read an IP-like field by its leaf tag name. Supports
        both V5 nested schema and direct text.

        For ``tag="ipAddress"`` the value lives in
        ``<IPAddress><ipAddress>X.X.X.X</ipAddress></IPAddress>`` on
        V5 IPC/NVR (the user's fleet), OR directly as the text of
        ``<IPAddress>...</IPAddress>`` on simpler V5 firmwares.

        For ``tag="subnetMask"`` it's either
        ``<IPAddress><subnetMask>X.X.X.X</subnetMask></IPAddress>``
        or direct.

        For ``tag="DefaultGateway"`` it's
        ``<IPAddress><DefaultGateway><ipAddress>X.X.X.X</ipAddress></DefaultGateway></IPAddress>``
        on V5 IPC, or the direct text of ``<DefaultGateway>`` on
        simpler firmwares.
        """
        if tag == "ipAddress":
            nested = iface.find("IPAddress/ipAddress")
            if nested is not None and nested.text:
                return nested.text.strip()
            return (iface.findtext("IPAddress") or "").strip() or None
        if tag == "subnetMask":
            nested = iface.find("IPAddress/subnetMask")
            if nested is not None and nested.text:
                return nested.text.strip()
            return (iface.findtext("subnetMask") or "").strip() or None
        if tag == "DefaultGateway":
            for path in (
                "IPAddress/DefaultGateway/ipAddress",
                "IPAddress/DefaultGateway",
                "DefaultGateway",
            ):
                found = iface.find(path)
                if found is not None and found.text:
                    return found.text.strip()
            return None
        # Generic.
        nested = iface.find(f"IPAddress/{tag}")
        if nested is not None and nested.text:
            return nested.text.strip()
        return (iface.findtext(tag) or "").strip() or None

    def _mac_address(iface: ET.Element) -> str | None:
        nested = iface.find("Link/MACAddress")
        if nested is not None and nested.text:
            return nested.text.strip()
        direct = iface.findtext("MACAddress")
        if direct:
            return direct.strip()
        return None

    def _mtu(iface: ET.Element) -> int | None:
        link_mtu = _safe_int_mb(_xml_text(iface, "Link/MTU"))
        if link_mtu is not None:
            return link_mtu
        return _safe_int_mb(_xml_text(iface, "MTU"))

    interfaces: list[dict[str, Any]] = []

    # Determine the container element. Try wrapper shapes first,
    # then the root itself, then direct <NetworkInterface>
    # children of root.
    container = root.find(".//NetworkInterfaceList")
    if container is None:
        container = root.find(".//NetworkInterfaces")
    if container is None:
        # Maybe root IS the list (its tag is one of those).
        if root.tag.split("}")[-1] in ("NetworkInterfaceList", "NetworkInterfaces"):
            container = root
        else:
            # Last resort: pull all <NetworkInterface> from anywhere
            # in the tree. If the user has them as direct children,
            # findall(".//NetworkInterface") finds them too.
            ni_elements = root.findall(".//NetworkInterface")
            if not ni_elements:
                return []
            # Build an ad-hoc container so the loop below is uniform.
            container = ET.Element("list")
            for ni in ni_elements:
                container.append(ni)

    for ni in container:
        # Skip non-NetworkInterface children (defensive).
        tag = ni.tag.split("}")[-1]
        if tag != "NetworkInterface":
            continue
        interfaces.append({
            "id": _xml_text(ni, "id"),
            "ip_address": _ip_field(ni, "ipAddress"),
            "subnet_mask": _ip_field(ni, "subnetMask"),
            "default_gateway": _ip_field(ni, "DefaultGateway"),
            "mac_address": _mac_address(ni),
            "mtu": _mtu(ni),
        })
    return interfaces


def parse_storage(root: ET.Element | None) -> dict[str, Any]:
    """Three Hikvision shapes are accepted (mirrors coordinator._parse_storage).

    - V5 NVR aggregate: direct <totalCapacity>/<usedCapacity>/<freeCapacity>
      (MB).
    - V4 NVR per-HDD list: <hddList><HDD><size>/<freeSize> in BYTES.
    - V5 IPC per-HDD list: <hddList><hdd><capacity>/<freeSpace> in MB
      (lowercase tags).
    """
    empty = {
        "total_mb": None,
        "used_mb": None,
        "free_mb": None,
        "status": "unknown",
    }
    if root is None:
        return empty
    total_mb = _safe_int_mb(_xml_text(root, "totalCapacity"))
    used_mb = _safe_int_mb(_xml_text(root, "usedCapacity"))
    free_mb = _safe_int_mb(_xml_text(root, "freeCapacity"))
    if total_mb is not None or used_mb is not None or free_mb is not None:
        return {
            "total_mb": total_mb,
            "used_mb": used_mb,
            "free_mb": free_mb,
            "status": _xml_text(root, "status") or "unknown",
        }

    # Per-HDD path. V4 NVR has <HDD>; V5 IPC has <hdd> (lowercase).
    hdds = root.findall(".//HDD")
    if not hdds:
        hdds = root.findall(".//hdd")
    if not hdds:
        return empty

    total_units = 0
    free_units = 0
    seen = False
    is_bytes = False
    for hdd in hdds:
        # V4 uses <size>/<freeSize>; V5 IPC uses <capacity>/<freeSpace>.
        size_raw = _safe_int_mb(_xml_text(hdd, "size"))
        if size_raw is None:
            size_raw = _safe_int_mb(_xml_text(hdd, "capacity"))
        else:
            is_bytes = True
        free_raw = _safe_int_mb(_xml_text(hdd, "freeSize"))
        if free_raw is None:
            free_raw = _safe_int_mb(_xml_text(hdd, "freeSpace"))
        if size_raw is None and free_raw is None:
            continue
        seen = True
        if size_raw is not None:
            total_units += size_raw
        if free_raw is not None:
            free_units += free_raw
    if not seen:
        return empty
    if is_bytes:
        # free==0 (full disk) used to leave used_mb=None because of
        # falsy ``and``. v0.6.33 fix: ``is not None`` on free.
        return {
            "total_mb": round(total_units / 1_000_000, 1) if total_units else None,
            "used_mb": (
                round((total_units - free_units) / 1_000_000, 1)
                if total_units and free_units is not None
                else None
            ),
            "free_mb": round(free_units / 1_000_000, 1) if free_units is not None else None,
            "status": _xml_text(root, "status") or "unknown",
        }
    return {
        "total_mb": round(total_units, 1) if total_units else None,
        "used_mb": (
            round(total_units - free_units, 1)
            if total_units and free_units is not None
            else None
        ),
        "free_mb": round(free_units, 1) if free_units is not None else None,
        "status": _xml_text(root, "status") or "unknown",
    }


# ---------------------------------------------------------------------------
# Mode 1: live device
# ---------------------------------------------------------------------------


def _save_capture(
    host: str,
    endpoint: str,
    raw: str,
    capture_dir: str | None = None,
) -> str:
    """Write a raw XML capture to disk for offline analysis.

    Returns the path written. Saved as UTF-8 with no further mutation
    so the bytes on disk match what the device sent (minus any host
    transport-encoded wrapping). The filename embeds the host and
    endpoint so multiple probes don't clobber each other.
    """
    import datetime as _dt
    base = capture_dir or os.path.join(os.getcwd(), "diagnostic_captures")
    os.makedirs(base, exist_ok=True)
    safe_ep = endpoint.strip("/").replace("/", "_").replace(" ", "_") or "root"
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    # Drop query fragment, hash host to keep FS-clean on Windows.
    host_tag = "".join(c if c.isalnum() else "_" for c in host)[:40]
    path = os.path.join(base, f"{host_tag}__{safe_ep}__{ts}.xml")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(raw)
    return path


def _print_top_level_tags(root: ET.Element, max_lines: int = 60) -> None:
    """Print the structure of ``root`` one tag per line so the user can
    spot unknown schema names. Includes element children only (skips
    text / tail)."""
    print(f"  top-level structure ({len(list(root))} direct children):")
    seen = 0
    def _walk(e: ET.Element, depth: int) -> None:
        nonlocal seen
        if seen >= max_lines:
            return
        # Show this element + attributes.
        attrs = " ".join(
            f"@{k}={v[:40]!r}" for k, v in e.attrib.items()
        ) if e.attrib else ""
        tag = e.tag.split("}")[-1]
        text_repr = ""
        if e.text and e.text.strip():
            text_repr = f" = {e.text.strip()[:60]!r}"
        indent = "    " + "  " * depth
        print(f"{indent}<{tag}> {attrs}{text_repr}")
        seen += 1
        for c in e:
            _walk(c, depth + 1)
            if seen >= max_lines:
                return
    _walk(root, 0)
    if seen >= max_lines:
        print(f"    ... (truncated at {max_lines} elements)")


def run_live(args) -> dict[str, Any]:
    """Connect to a real device and probe capabilities + network + storage."""
    print(f"=== Probing {args.host} (https={args.use_https}) ===\n")
    concerns: list[str] = []
    out: dict[str, Any] = {"concerns": concerns}

    # Capabilities
    print("--- /ISAPI/System/capabilities ---")
    cap_endpoints = (
        "/ISAPI/System/capabilities",
        "/ISAPI/System/Capabilities",
    )
    cap_xml = None
    cap_ep_used = None
    for ep in cap_endpoints:
        try:
            cap_xml = fetch_isapi(
                args.host, args.user, args.password, ep,
                use_https=args.use_https, timeout=args.timeout,
            )
            print(f"  OK ({ep})")
            cap_ep_used = ep
            break
        except IsapiFetchError as e:
            print(f"  fail ({ep}): {e}")
    if cap_xml is None:
        concerns.append(
            "capabilities endpoint not present on this device — "
            "capability_video_input_channels will stay unknown"
        )
        out["capabilities"] = None
    else:
        capture_path = _save_capture(args.host, cap_ep_used, cap_xml)
        print(f"  raw saved → {capture_path}")
        try:
            cap_root = _strip_xmlns(cap_xml)
            cap = parse_capabilities(cap_root)
            print(f"  parsed: {json.dumps(cap, ensure_ascii=False)}")
            if cap.get("video_input_channels") is None:
                concerns.append(
                    "capabilities XML parsed but no candidate tag "
                    "matched for video_input_channels — XML may use a "
                    "different field name; please paste the XML in a "
                    "GitHub issue."
                )
                _print_top_level_tags(cap_root)
            out["capabilities"] = cap
            out["capabilities_capture"] = capture_path
        except Exception as e:
            print(f"  parse failed: {e}")
            concerns.append(f"capabilities XML parse failed: {e}")
            out["capabilities"] = None

    # Network interfaces (try multiple endpoints)
    print()
    print("--- /ISAPI/System/Network/interfaces ---")
    net_endpoints = (
        "/ISAPI/System/Network/interfaces",
        "/ISAPI/System/network/interfaces",
        "/ISAPI/Networking/interfaces",
        "/ISAPI/System/NetworkInterface",
    )
    net_xml = None
    net_ep_used = None
    for ep in net_endpoints:
        try:
            net_xml = fetch_isapi(
                args.host, args.user, args.password, ep,
                use_https=args.use_https, timeout=args.timeout,
            )
            print(f"  OK ({ep})")
            net_ep_used = ep
            break
        except IsapiFetchError as e:
            print(f"  fail ({ep}): {e}")
    if net_xml is None:
        concerns.append(
            "network interfaces endpoint not present — NIC 2 sensor "
            "will not register. Try the live device again with a "
            "different firmware or check if device has 2 NICs."
        )
        out["network_interfaces"] = []
    else:
        capture_path = _save_capture(args.host, net_ep_used, net_xml)
        print(f"  raw saved → {capture_path}")
        try:
            net_root = _strip_xmlns(net_xml)
            ifaces = parse_network_interfaces(net_root)
            print(f"  parsed {len(ifaces)} interface(s):")
            for i, iface in enumerate(ifaces):
                print(f"    [{i}] {json.dumps(iface, ensure_ascii=False)}")
            if len(ifaces) >= 2:
                print(
                    "  >>> Dual-NIC detected: integration should "
                    "register network_2_* sensors on next reload."
                )
            elif len(ifaces) == 1:
                concerns.append(
                    "only 1 network interface parsed — if your NVR "
                    "has 2 NICs, the XML may use a different element "
                    "structure. Paste the XML in a GitHub issue."
                )
                _print_top_level_tags(net_root)
            out["network_interfaces"] = ifaces
            out["network_interfaces_capture"] = capture_path
        except Exception as e:
            print(f"  parse failed: {e}")
            concerns.append(f"network interfaces parse failed: {e}")
            out["network_interfaces"] = []

    # Storage (try multiple endpoints)
    print()
    print("--- storage (multiple endpoints) ---")
    storage_endpoints = (
        "/ISAPI/ContentMgmt/storage",
        "/ISAPI/System/Storage/hardDisks",
        "/ISAPI/ContentMgmt/storage/hddList",
    )
    stor_xml = None
    stor_ep_used = None
    for ep in storage_endpoints:
        try:
            stor_xml = fetch_isapi(
                args.host, args.user, args.password, ep,
                use_https=args.use_https, timeout=args.timeout,
            )
            print(f"  OK ({ep})")
            stor_ep_used = ep
            break
        except IsapiFetchError as e:
            print(f"  fail ({ep}): {e}")
    if stor_xml is None:
        concerns.append(
            "storage endpoint not present — storage_*_gb sensors "
            "will stay unknown. Expected on IPCs without HDD; on NVR "
            "this means the firmware refuses all storage endpoints."
        )
        out["storage"] = None
    else:
        capture_path = _save_capture(args.host, stor_ep_used, stor_xml)
        print(f"  raw saved → {capture_path}")
        try:
            stor_root = _strip_xmlns(stor_xml)
            stor = parse_storage(stor_root)
            print(f"  parsed: {json.dumps(stor, ensure_ascii=False)}")
            if stor.get("total_mb") is None:
                concerns.append(
                    "storage XML parsed but no total/used/free "
                    "extracted — XML may use a different schema. "
                    "Paste the XML in a GitHub issue."
                )
                _print_top_level_tags(stor_root)
            out["storage"] = stor
            out["storage_capture"] = capture_path
        except Exception as e:
            print(f"  parse failed: {e}")
            concerns.append(f"storage parse failed: {e}")
            out["storage"] = None

    return out


# ---------------------------------------------------------------------------
# Mode 2: paste XML
# ---------------------------------------------------------------------------


def run_paste(args) -> dict[str, Any]:
    if not args.xml_file:
        print("ERROR: --xml-file required when --paste is set", file=sys.stderr)
        sys.exit(2)
    xml_text = open(args.xml_file, encoding="utf-8").read()
    print(f"=== Parsing {args.endpoint} XML from {args.xml_file} ===\n")
    concerns: list[str] = []
    try:
        root = _strip_xmlns(xml_text)
    except Exception as e:
        print(f"XML parse failed: {e}")
        return {"concerns": [f"XML parse failed: {e}"]}

    if args.endpoint == "capabilities":
        result = parse_capabilities(root)
        print(f"Parsed: {json.dumps(result, ensure_ascii=False)}")
        if result.get("video_input_channels") is None:
            concerns.append(
                "video_input_channels is None — none of the "
                "candidate tags matched. The XML uses a tag name "
                "we don't know about. Please paste the full XML "
                "in a GitHub issue."
            )
            # List all top-level tags for debugging
            print("\nAll tags containing 'Channel' or 'Video':")
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                if "channel" in tag.lower() or "video" in tag.lower():
                    print(f"  <{tag}>={el.text!r}")
    elif args.endpoint == "network":
        ifaces = parse_network_interfaces(root)
        print(f"Parsed {len(ifaces)} interface(s):")
        for i, iface in enumerate(ifaces):
            print(f"  [{i}] {json.dumps(iface, ensure_ascii=False)}")
        if len(ifaces) < 2:
            concerns.append(
                f"only {len(ifaces)} interface(s) parsed — expected "
                "≥2 for dual-NIC NVR. The XML structure may differ."
            )
            print("\nAll <NetworkInterface*> / <IPAddress> tags:")
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                if any(k in tag.lower() for k in (
                    "networkinterface", "ipaddress", "subnet",
                    "defaultgateway", "macaddress", "link"
                )):
                    print(f"  <{tag}>={el.text!r}")
    elif args.endpoint == "storage":
        result = parse_storage(root)
        print(f"Parsed: {json.dumps(result, ensure_ascii=False)}")
        if result.get("total_mb") is None:
            concerns.append("total_mb is None — storage XML schema unrecognised")
            print("\nAll <HDD> / <Capacity> / <size> / <freeSize> tags:")
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                if any(k in tag.lower() for k in (
                    "hdd", "capacity", "size", "freesize", "storage"
                )):
                    print(f"  <{tag}>={el.text!r}")
    else:
        print(f"Unknown endpoint: {args.endpoint!r}")
        sys.exit(2)

    if concerns:
        print(f"\n=== Concerns ({len(concerns)}) ===")
        for c in concerns:
            print(f"  ! {c}")
    return {"concerns": concerns}


def _default_ha_config_path() -> str:
    """Return the most likely HA core.config_entries location."""
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA", "")
    candidates = [
        # Linux / macOS defaults
        os.path.join(home, ".homeassistant", ".storage", "core.config_entries"),
        os.path.join(home, "homeassistant", ".storage", "core.config_entries"),
        os.path.join(home, "config", ".storage", "core.config_entries"),
        # Windows defaults
        os.path.join(home, ".homeassistant", ".storage", "core.config_entries"),
        os.path.join(appdata, ".homeassistant", ".storage", "core.config_entries"),
        os.path.join("C:\\", "config", ".storage", "core.config_entries"),
        # HAOS / Docker volume mounts (less common on Windows desktop
        # but possible if user has HA in WSL)
        "/config/.storage/core.config_entries",
        "/root/.homeassistant/.storage/core.config_entries",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]  # default guess


def _list_ha_entries(path: str | None) -> None:
    """Print hikvision_isapi_performance entries in the HA config."""
    if path is None:
        path = _default_ha_config_path()
    if not os.path.exists(path):
        print(f"HA config not found at: {path}")
        print("Pass --from-ha-config <PATH> explicitly.")
        return
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception as e:
        print(f"Failed to parse {path}: {e}")
        return
    entries = data.get("data", {}).get("entries", [])
    found = False
    for entry in entries:
        if entry.get("domain") != "hikvision_isapi_performance":
            continue
        found = True
        entry_id = entry.get("entry_id", "?")[:8]
        d = entry.get("data", {})
        print(f"  entry_id={entry_id}... title={entry.get('title', '?')!r}")
        print(f"    host        = {d.get('host', '?')!r}")
        print(f"    username    = {d.get('username', '?')!r}")
        print(f"    use_https   = {d.get('use_https', '?')!r}")
        print(f"    password    = {'*** set (len=' + str(len(d.get('password', ''))) + ')' if d.get('password') else '<EMPTY>'}")
        print(f"    verify_ssl  = {d.get('verify_ssl', '?')!r}")
        print(f"    port        = {d.get('port', '?')!r}")
    if not found:
        print("No hikvision_isapi_performance entries found in this HA config.")


def _resolve_creds_from_ha(
    path: str | None,
) -> tuple[str, str, str, bool] | None:
    """Return (host, user, password, use_https) for the first matching
    hikvision_isapi_performance entry that has a non-empty password.
    """
    if path is None:
        path = _default_ha_config_path()
    if not os.path.exists(path):
        print(f"HA config not found at: {path}")
        return None
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception as e:
        print(f"Failed to parse {path}: {e}")
        return None
    for entry in data.get("data", {}).get("entries", []):
        if entry.get("domain") != "hikvision_isapi_performance":
            continue
        d = entry.get("data", {})
        host = d.get("host", "")
        user = d.get("username", "")
        password = d.get("password", "")
        use_https = bool(d.get("use_https", False))
        if host and user and password:
            return (host, user, password, use_https)
    return None


def main():
    p = argparse.ArgumentParser(
        description="Diagnose why hikvision_isapi_performance sensors "
                    "show 'unknown' on your Hikvision device."
    )
    p.add_argument("--host", help="device IP (live mode)")
    p.add_argument("--user", help="ISAPI username (live mode)")
    p.add_argument("--password", help="ISAPI password (live mode)")
    p.add_argument(
        "--https", action="store_true", dest="use_https",
        help="use https (default: http)",
    )
    p.add_argument("--timeout", type=float, default=8.0,
                   help="HTTP timeout in seconds (default 8)")
    p.add_argument("--paste", action="store_true",
                   help="parse captured XML from --xml-file instead "
                        "of probing a live device")
    p.add_argument("--xml-file", help="path to captured XML response "
                                       "(for --paste mode)")
    p.add_argument("--endpoint", choices=("capabilities", "network", "storage"),
                   help="which endpoint the XML is from (for --paste mode)")
    p.add_argument(
        "--from-ha-config",
        nargs="?",
        const="AUTO",
        default=None,
        metavar="PATH",
        help="read host / user / password from HA core.config_entries "
             "(default path: ~/.homeassistant/.storage/core.config_entries). "
             "Pass an explicit path or use the default. Use this if you "
             "can't remember the password — it's already stored in HA.",
    )
    p.add_argument(
        "--list-ha-entries",
        nargs="?",
        const="AUTO",
        default=None,
        metavar="PATH",
        help="list hikvision_isapi_performance entries in the HA config "
             "and exit",
    )
    args = p.parse_args()

    # Resolve constants
    ha_path = None
    if args.from_ha_config is not None:
        ha_path = (
            None if args.from_ha_config == "AUTO"
            else args.from_ha_config
        )
    if args.list_ha_entries is not None:
        list_path = (
            None if args.list_ha_entries == "AUTO"
            else args.list_ha_entries
        )
        _list_ha_entries(list_path)
        return

    # Auto-fill from HA config if requested (and CLI args not provided)
    if ha_path is not None or (
        not (args.host and args.user and args.password)
    ):
        creds = _resolve_creds_from_ha(ha_path)
        if creds is not None:
            if not args.host:
                args.host = creds[0]
            if not args.user:
                args.user = creds[1]
            if not args.password:
                args.password = creds[2]
            if not args.use_https:
                args.use_https = creds[3]
            print(
                f"[from HA config] host={args.host} user={args.user} "
                f"use_https={args.use_https}\n"
            )

    if args.paste:
        result = run_paste(args)
    else:
        if not (args.host and args.user and args.password):
            print(
                "ERROR: --host --user --password required for live mode "
                "(or use --from-ha-config to read from HA storage)",
                file=sys.stderr,
            )
            sys.exit(2)
        result = run_live(args)

    # Final summary
    concerns = result.get("concerns", [])
    print(f"\n=== Summary ===")
    print(f"Concerns: {len(concerns)}")
    for c in concerns:
        print(f"  ! {c}")
    if not concerns:
        print("  (none — your device XML should produce working sensors)")
        print("    If sensors still show 'unknown', check:")
        print("    1. Did you Reload the integration after upgrading?")
        print("    2. Are scan_interval + access permissions correct?")
        print("    3. Run with --paste + the actual XML if live mode "
              "still misses fields.")
    sys.exit(0)


if __name__ == "__main__":
    main()