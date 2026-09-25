"""Regression tests for v0.6.25 — memory unit normalisation.

User feedback after v0.6.24 (via attached analysis document):

    海康同一条 ISAPI 接口 ``/ISAPI/System/status``，不同固件、设备
    类型返回的数据格式、计量单位、可用性都不一致

Concretely:
- V4 NVR (DS-7708N-I4 V4.1.18): both ``memoryUsage`` and
  ``memoryAvailable`` are in decimal MB.
- V5 IPC (DS-FB2127 V5.2.2): ``memoryUsage`` in MB but
  ``memoryAvailable`` in KB. From real XML:
  ``<memoryUsage>61</memoryUsage>`` (MB)
  ``<memoryAvailable>224756</memoryAvailable>`` (KB ≈ 219 MB)

Pre-v0.6.25 the parser stored both as raw integers, then
``_memory_usage_percent`` computed ``used / (used + available)``
treating them as the same unit. On V5 IPC this produced:
  61 / (61 + 224756) * 100 = 0.027% memory usage — wildly wrong.

Fix: heuristic in ``_parse_system_status``: if
``memoryAvailable > memoryUsage * 50`` then ``memoryAvailable``
is in KB, convert to MB. The 50x threshold is conservative —
V4 NVR's used/available are within the same order of magnitude
(typical ~1.8x), so they fall well below it.

Tests:
- V5 IPC XML: ``memoryAvailable`` 224664 KB → 219 MB
- V4 NVR XML: both stay in MB (no false conversion)
- Edge case: equal usage/availability doesn't trigger conversion
- Sanity: ``_memory_usage_percent`` computes sensible percentage
"""

from __future__ import annotations

import sys
from pathlib import Path


_COORD_SRC = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance\coordinator.py"
)


def _load_coordinator():
    """Load ``coordinator`` module into sys.modules."""
    sys.path.insert(0, str(_COORD_SRC.parent.parent))
    from custom_components.hikvision_isapi_performance import coordinator as coord
    return coord


def _parse_xml(xml: str):
    """Strip xmlns and parse XML, returning root Element."""
    import xml.etree.ElementTree as ET
    from custom_components.hikvision_isapi_performance.isapi_client import (
        _strip_xmlns,
    )
    return ET.fromstring(_strip_xmlns(xml))


# ---- V5 IPC: memoryAvailable in KB converted to MB ----


def test_v0625_v5_ipc_memory_available_converted_from_kb():
    """V5 IPC XML with ``memoryAvailable=224664`` (KB) must convert to ~219 MB."""
    coord = _load_coordinator()
    xml = """<DeviceStatus>
<MemoryList>
<Memory>
<memoryUsage>61</memoryUsage>
<memoryAvailable>224664</memoryAvailable>
</Memory>
</MemoryList>
</DeviceStatus>"""
    status = coord._parse_system_status(_parse_xml(xml))
    assert status["memoryUsage"] == "61", (
        "v0.6.25: V5 IPC memoryUsage must stay 61 (MB) "
        f"(got {status['memoryUsage']})"
    )
    assert status["memoryAvailable"] == "219", (
        f"v0.6.25: V5 IPC memoryAvailable must convert 224664 KB → "
        f"~219 MB (got {status['memoryAvailable']}). Pre-v0.6.25 "
        f"the raw value passed through, breaking "
        f"_memory_usage_percent."
    )


# ---- V4 NVR: both fields already in MB, no conversion ----


def test_v0625_v4_nvr_memory_units_unchanged():
    """V4 NVR XML with both fields in MB must NOT trigger conversion.

    V4 NVR ``DS-7708N-I4``: ``memoryUsage=728.234375`` (MB) and
    ``memoryAvailable=402.613281`` (MB). Ratio ≈ 0.55x, well
    below the 50x threshold — no conversion should fire.

    ``_safe_int_mb`` uses ``int(round(...))`` so 402.613281
    becomes 403 — that's pre-existing rounding behaviour, not a
    v0.6.25 regression. We assert against the rounded value.
    """
    coord = _load_coordinator()
    xml = """<DeviceStatus>
<MemoryList>
<Memory>
<memoryUsage>728.234375</memoryUsage>
<memoryAvailable>402.613281</memoryAvailable>
</Memory>
</MemoryList>
</DeviceStatus>"""
    status = coord._parse_system_status(_parse_xml(xml))
    # _safe_int_mb rounds: round(728.234375) = 728, round(402.613281) = 403
    assert status["memoryUsage"] == "728"
    assert status["memoryAvailable"] == "403"


# ---- Edge cases ----


def test_v0625_equal_usage_and_availability_no_conversion():
    """When usage and availability are equal, no conversion.

    Sanity check on the threshold: ratio = 1.0 < 50, so the
    conversion is skipped. Without this test, a buggy threshold
    could silently flip equal values.
    """
    coord = _load_coordinator()
    xml = """<DeviceStatus>
<MemoryList>
<Memory>
<memoryUsage>512</memoryUsage>
<memoryAvailable>512</memoryAvailable>
</Memory>
</MemoryList>
</DeviceStatus>"""
    status = coord._parse_system_status(_parse_xml(xml))
    assert status["memoryUsage"] == "512"
    assert status["memoryAvailable"] == "512"


def test_v0625_zero_usage_skips_conversion():
    """If memoryUsage is 0 (unparseable / missing), no conversion.

    The threshold divides by ``memoryUsage`` — must guard against
    division by zero. Pre-v0.6.25 if memoryUsage was 0 or
    unparseable, the heuristic would have run division on it.
    """
    coord = _load_coordinator()
    xml = """<DeviceStatus>
<MemoryList>
<Memory>
<memoryUsage>0</memoryUsage>
<memoryAvailable>500000</memoryAvailable>
</Memory>
</MemoryList>
</DeviceStatus>"""
    status = coord._parse_system_status(_parse_xml(xml))
    # No division-by-zero crash; the value passes through.
    assert status["memoryUsage"] == "0"
    # memoryAvailable = 500000 (KB equivalent, but we don't convert
    # because usage is 0 — there's no ratio to compare against).
    assert status["memoryAvailable"] == "500000"


# ---- Integration: end-to-end memory_usage_percent is now sane ----


def test_v0625_memory_usage_percent_correct_on_v5_ipc():
    """V5 IPC's actual memory % should be ~21% (61 used, 219 avail),
    NOT ~0.027% as pre-v0.6.25 reported.
    """
    from dataclasses import dataclass

    # _memory_usage_percent lives in sensor.py. Source-load it
    # along with the helper ``_safe_int`` it depends on, since
    # the conftest's SensorEntityDescription stub can't be
    # subclassed by the dataclass decorator.
    sensor_src = (
        Path(r"C:\Users\43457\Desktop\hikvision-isapi")
        / "custom_components"
        / "hikvision_isapi_performance"
        / "sensor.py"
    ).read_text(encoding="utf-8-sig")
    body_lines = sensor_src.splitlines()
    ns: dict = {}
    # Pull _safe_int AND _memory_usage_percent into the namespace.
    for fn_name in ("_safe_int", "_memory_usage_percent"):
        start_idx = None
        for i, line in enumerate(body_lines):
            if line.startswith(f"def {fn_name}("):
                start_idx = i
                break
        assert start_idx is not None, f"{fn_name} not found in sensor.py"
        end_idx = start_idx + 1
        while end_idx < len(body_lines):
            nxt = body_lines[end_idx]
            if (
                nxt.startswith("def ")
                or nxt.startswith("class ")
                or nxt.startswith("@")
            ):
                break
            end_idx += 1
        func_src = "\n".join(body_lines[start_idx:end_idx])
        exec(func_src, ns)
    pct_fn = ns["_memory_usage_percent"]

    @dataclass
    class _Data:
        system_status: dict

    data = _Data(system_status={
        "memoryUsage": "61",
        "memoryAvailable": "219",
        "cpuUtilization": "48",
        "uptime": "145954",
        "rebootCount": None,
        "cpuDescription": None,
    })
    pct = pct_fn(data)
    assert pct is not None
    # 61 / (61 + 219) * 100 = 21.78...
    assert 20 < pct < 23, (
        f"v0.6.25: V5 IPC memory % should be ~21.8% "
        f"(got {pct}%). Pre-v0.6.25 it was ~0.03%."
    )


# ---- Helpers ----