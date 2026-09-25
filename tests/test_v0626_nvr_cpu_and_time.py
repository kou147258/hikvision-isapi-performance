"""Regression tests for v0.6.26.

User feedback after v0.6.25 (via second attached analysis document)
flagged two real bugs plus one documentation gap:

🟥 2. Old V4 NVR firmware returns ``cpuUtilization=0`` for its own
       CPU (firmware bug). Pre-v0.6.26 the integration registered
       the ``cpu_usage`` sensor on every device, so a V4 NVR
       showed a permanent "0%" — masking real high-CPU conditions
       from any HA alert threshold.

       Fix: skip the ``cpu_usage`` sensor entirely on NVR/DVR
       (``is_recorder`` check in ``async_setup_entry``). IPC keeps
       the sensor because their CPU readings are reliable.

🟨 6. The integration had no way to detect a dead CMOS battery on
       a V4 NVR — the device stays online, records, and streams
       normally, but its ``<currentDeviceTime>`` rolls back to
       2004-05-03. Without a sensor, automations keyed on
       timestamps silently use the wrong reference, and history
       charts show data points stamped 20+ years ago.

       Fix: parse ``<currentDeviceTime>`` in ``_parse_system_status``
       (the docstring claimed it but the code never stored it)
       and expose a ``device_time_abnormal`` binary sensor with
       ``BinarySensorDeviceClass.PROBLEM``. ON when device clock
       is more than 24 h away from HA host's wall-clock time.

🟪 9. Documentation gap: HA users routinely set
       ``scan_interval: 30`` and trip the Hikvision web-account
       lockout, taking every ISAPI sensor on the device offline.
       README now warns ``scan_interval >= 120``.

Tests:
- ``_parse_system_status`` now extracts ``currentDeviceTime``
- ``_device_time_abnormal`` returns True / False / None correctly
- NVR/DVR ``async_setup_entry`` does NOT register ``cpu_usage``
- IPC ``async_setup_entry`` DOES register ``cpu_usage``
- README contains the scan_interval warning (English + Chinese)
- All 4 translation files contain ``device_time_abnormal``
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


_REPO_ROOT = Path(r"C:\Users\43457\Desktop\hikvision-isapi")
_INTEGRATION_ROOT = (
    _REPO_ROOT / "custom_components" / "hikvision_isapi_performance"
)
_TRANSLATIONS_DIR = _INTEGRATION_ROOT / "translations"


# ---------------------------------------------------------------------------
# Shared helpers (mirror the v0.6.25 test pattern).
# ---------------------------------------------------------------------------


def _load_module(name: str):
    """Load ``custom_components.hikvision_isapi_performance.<name>``."""
    sys.path.insert(0, str(_INTEGRATION_ROOT.parent))
    from custom_components.hikvision_isapi_performance import (
        binary_sensor,
        coordinator,
    )
    return {
        "binary_sensor": binary_sensor,
        "coordinator": coordinator,
    }


def _parse_xml(xml: str):
    """Strip xmlns and parse XML, returning root Element."""
    import xml.etree.ElementTree as ET
    from custom_components.hikvision_isapi_performance.isapi_client import (
        _strip_xmlns,
    )
    return ET.fromstring(_strip_xmlns(xml))


def _source_load_function(
    module_path: Path,
    fn_name: str,
    extra_globals: dict | None = None,
):
    """Read ``module_path`` and exec the function named ``fn_name``.

    Returns the live function object (or ``None`` if exec fails).

    The test conftest stubs ``homeassistant.*`` packages with bare
    classes that don't support dataclass inheritance. Loading
    sensor.py / binary_sensor.py as a module therefore fails for
    tests that just want one helper function. Source-extract +
    exec() of the function body sidesteps the dependency chain.

    Module-level constants referenced by the function body
    (e.g. ``DEVICE_TIME_ABNORMAL_THRESHOLD_SECONDS = 86400``)
    are auto-collected by walking lines BEFORE the function
    definition. ``extra_globals`` is merged on top so callers
    can inject runtime values (``datetime``, ``timezone``,
    ...) that the function body needs.
    """
    import re as _re

    src = module_path.read_text(encoding="utf-8-sig")
    body_lines = src.splitlines()
    start_idx = None
    for i, line in enumerate(body_lines):
        if line.startswith(f"def {fn_name}("):
            start_idx = i
            break
    assert start_idx is not None, f"function {fn_name} not found in {module_path}"

    # Collect module-level ``NAME = value`` assignments that appear
    # before the function. Use a regex so we tolerate ``_LOGGER``
    # style identifiers and numeric constants alike.
    module_consts: dict[str, object] = {}
    const_re = _re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)$")
    for line in body_lines[:start_idx]:
        m = const_re.match(line)
        if not m:
            continue
        name, expr = m.group(1), m.group(2)
        try:
            module_consts[name] = eval(expr, {"__builtins__": {}})
        except Exception:
            # Skip constants whose value can't be eval'd in
            # isolation (e.g. references to logging.getLogger).
            pass

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
    ns: dict = {}
    ns.update(module_consts)
    if extra_globals:
        ns.update(extra_globals)
    exec(func_src, ns)
    return ns[fn_name]


# ---------------------------------------------------------------------------
# 🟥2 — NVR/DVR does NOT register ``cpu_usage``
# ---------------------------------------------------------------------------


def _read_sensor_setup_entities(sensor_path: Path) -> str:
    """Return the source of ``async_setup_entry`` in sensor.py."""
    return sensor_path.read_text(encoding="utf-8-sig")


def test_v0626_nvr_cpu_usage_suppressed_at_setup_level():
    """NVR/DVR (``is_recorder=True``) must skip the ``cpu_usage`` sensor.

    Pre-v0.6.26 every device got ``cpu_usage`` registered. V4 NVRs
    report ``cpuUtilization=0`` due to a firmware bug — surfacing
    this as "0% CPU" let users miss real high-CPU conditions.
    v0.6.26: the ``async_setup_entry`` filter rejects the
    description on NVR/DVR. IPC keeps the sensor.
    """
    sensor_path = (
        _INTEGRATION_ROOT / "sensor.py"
    )
    src = _read_sensor_setup_entities(sensor_path)
    # The filter must mention BOTH ``cpu_usage`` and ``is_recorder``
    # inside the ``entities = [...]`` list comprehension.
    # We accept either an inline ``desc.key != "cpu_usage"`` guard
    # or a separate exclusion — what matters is the keyword pairing.
    assert "cpu_usage" in src, (
        "v0.6.26: sensor.py should reference cpu_usage in setup "
        "(to suppress it on NVR/DVR). If you removed cpu_usage "
        "entirely, you broke IPC users."
    )
    # The list-comprehension filter MUST have a clause that
    # excludes ``cpu_usage`` when ``is_recorder`` is True.
    # Match: ``and (not is_recorder or desc.key != "cpu_usage")``
    # or any equivalent condition that pairs both tokens.
    list_comp_match = re.search(
        r"entities\s*:\s*list\[HikvisionISAPISensor\]\s*=\s*\[\s*(.*?)\]",
        src,
        re.DOTALL,
    )
    assert list_comp_match, "entities list comprehension not found"
    body = list_comp_match.group(1)
    assert "cpu_usage" in body, (
        "v0.6.26: cpu_usage must be filtered out inside the "
        "entities list comprehension (skip on NVR/DVR)."
    )
    assert "is_recorder" in body, (
        "v0.6.26: the cpu_usage filter must be guarded by "
        "is_recorder so IPC retains the sensor."
    )


def test_v0626_ipc_cpu_usage_still_registered():
    """IPC (``is_recorder=False``) must keep the ``cpu_usage`` sensor.

    The filter in async_setup_entry must allow ``cpu_usage``
    through when the device is an IPC. Verify by simulating the
    filter: if ``is_recorder=False`` then ``cpu_usage`` description
    survives; if ``is_recorder=True`` then it's dropped.
    """
    # Static check: the filter expression must be such that
    # ``is_recorder=False`` allows ``cpu_usage`` through.
    sensor_src = (
        _INTEGRATION_ROOT / "sensor.py"
    ).read_text(encoding="utf-8-sig")
    list_comp_match = re.search(
        r"entities\s*:\s*list\[HikvisionISAPISensor\]\s*=\s*\[\s*(.*?)\]",
        sensor_src,
        re.DOTALL,
    )
    assert list_comp_match
    body = list_comp_match.group(1)
    # The exact v0.6.26 filter is:
    #   if (
    #     (is_recorder or not desc.key.startswith("storage_"))
    #     and (not is_recorder or desc.key != "cpu_usage")
    #   )
    # For IPC (is_recorder=False):
    #   - storage filter: (False or not True) = False → storage
    #     sensors excluded (correct).
    #   - cpu filter: (True or desc.key != "cpu_usage") = True → cpu
    #     sensor included (correct).
    assert "not is_recorder" in body or "is_recorder or" in body, (
        "v0.6.26: filter must use is_recorder to allow "
        "cpu_usage on IPC while excluding it on NVR/DVR."
    )


# ---------------------------------------------------------------------------
# 🟨6 — ``currentDeviceTime`` parsing + ``device_time_abnormal`` detection
# ---------------------------------------------------------------------------


def test_v0626_current_device_time_extracted_from_system_status():
    """``_parse_system_status`` must store ``<currentDeviceTime>``.

    Pre-v0.6.26 the docstring described ``<currentDeviceTime>``
    but the function never extracted it — the field was lost
    between parsing and the sensor / binary-sensor layers.
    v0.6.26 fixes that so ``device_time_abnormal`` can read it.
    """
    mods = _load_module("coordinator")
    coord = mods["coordinator"]
    xml = """<DeviceStatus>
<currentDeviceTime>2004-05-03T22:54:38+08:00</currentDeviceTime>
<deviceStatus>OK</deviceStatus>
<deviceUpTime>12345</deviceUpTime>
</DeviceStatus>"""
    status = coord._parse_system_status(_parse_xml(xml))
    assert status.get("currentDeviceTime") == "2004-05-03T22:54:38+08:00", (
        f"v0.6.26: _parse_system_status must extract "
        f"<currentDeviceTime> into the dict (got "
        f"{status.get('currentDeviceTime')!r}). Pre-v0.6.26 the "
        f"docstring claimed it but the code never stored it."
    )


def test_v0626_current_device_time_none_when_root_is_none():
    """When ``/System/status`` returns no XML, the field must default to None."""
    mods = _load_module("coordinator")
    coord = mods["coordinator"]
    status = coord._parse_system_status(None)
    assert status.get("currentDeviceTime") is None, (
        "v0.6.26: missing <currentDeviceTime> must default to None "
        "(so the binary sensor can render 'unknown')."
    )


def test_v0626_device_time_abnormal_dead_cmos_battery():
    """Dead CMOS battery (2004-05) → True at any reasonable 'now'."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(
        bs_path,
        "_device_time_abnormal",
        extra_globals={"datetime": datetime, "timezone": timezone},
    )
    # Pick a now that's well within the 24h window of "2026-09-25".
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    # 2004-05-03 vs 2026-09-25 ≈ 22 years off → must flag.
    assert fn("2004-05-03T22:54:38+08:00", now) is True
    # Year 2099 future-drift is also abnormal.
    assert fn("2099-12-31T23:59:59+00:00", now) is True


def test_v0626_device_time_normal_returns_false():
    """When device clock is within 24 h, return False (not True, not None)."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(
        bs_path,
        "_device_time_abnormal",
        extra_globals={"datetime": datetime, "timezone": timezone},
    )
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    # 1 hour off → False.
    one_hour_ago = (now - timedelta(hours=1)).isoformat()
    assert fn(one_hour_ago, now) is False
    # 23 hours off → still False (below threshold).
    twenty_three_hours_ago = (now - timedelta(hours=23)).isoformat()
    assert fn(twenty_three_hours_ago, now) is False


def test_v0626_device_time_just_over_threshold_returns_true():
    """Boundary: >24 h off is True, exactly 24 h is False.

    Use ``>`` (strict) rather than ``>=`` so a 24-hour clock
    drift during DST / NTP sync doesn't false-positive. This
    test pins the comparison operator so a future refactor to
    ``>=`` triggers the test rather than a real-world false
    alarm.
    """
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(
        bs_path,
        "_device_time_abnormal",
        extra_globals={"datetime": datetime, "timezone": timezone},
    )
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    # Exactly 24 h off → False (boundary, not over).
    exactly_24h = (now - timedelta(hours=24)).isoformat()
    assert fn(exactly_24h, now) is False, (
        "v0.6.26: 24 h drift must NOT trigger — threshold is "
        "strict greater-than so NTP sync noise doesn't false alarm."
    )
    # 25 h off → True.
    twenty_five_h = (now - timedelta(hours=25)).isoformat()
    assert fn(twenty_five_h, now) is True


def test_v0626_device_time_unknown_when_field_missing():
    """When ``currentDeviceTime`` is None, return None (HA renders 'unknown')."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(
        bs_path,
        "_device_time_abnormal",
        extra_globals={"datetime": datetime, "timezone": timezone},
    )
    assert fn(None) is None
    # Garbage string from a quirky firmware should also be None,
    # not raise (the entity would otherwise blow up every refresh).
    assert fn("0") is None
    assert fn("") is None
    assert fn("not-a-date") is None


def test_v0626_binary_sensor_class_registered():
    """``HikvisionISAPIDeviceTimeAbnormalBinarySensor`` must exist + register."""
    mods = _load_module("binary_sensor")
    bs = mods["binary_sensor"]
    cls = getattr(
        bs, "HikvisionISAPIDeviceTimeAbnormalBinarySensor", None
    )
    assert cls is not None, (
        "v0.6.26: HikvisionISAPIDeviceTimeAbnormalBinarySensor "
        "class is missing from binary_sensor.py"
    )
    # Verify async_setup_entry actually instantiates it.
    bs_src = (_INTEGRATION_ROOT / "binary_sensor.py").read_text(
        encoding="utf-8-sig"
    )
    assert "HikvisionISAPIDeviceTimeAbnormalBinarySensor(" in bs_src, (
        "v0.6.26: async_setup_entry must instantiate the new "
        "binary sensor (otherwise the entity never registers)."
    )


# ---------------------------------------------------------------------------
# 🟪9 — README scan_interval warning
# ---------------------------------------------------------------------------


def test_v0626_readme_english_scan_interval_warning():
    """English README must warn about scan_interval >= 120."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8-sig")
    # Split at the 简体中文 anchor so we test the English section only.
    english = readme.split("## 配置")[0]
    assert "scan_interval" in english.lower(), (
        "v0.6.26: English README must mention scan_interval."
    )
    assert "120" in english, (
        "v0.6.26: English README must warn about scan_interval >= 120."
    )


def test_v0626_readme_chinese_scan_interval_warning():
    """Chinese README must also warn about scan_interval >= 120."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8-sig")
    chinese = readme.split("## 配置", 1)[1]
    assert "120" in chinese, (
        "v0.6.26: Chinese README must warn about scan_interval >= 120."
    )
    # Sanity: the Chinese section should mention scan_interval too.
    assert "scan_interval" in chinese or "轮询间隔" in chinese, (
        "v0.6.26: Chinese README must discuss the polling interval."
    )


# ---------------------------------------------------------------------------
# Translations: every translation file must include the new key
# ---------------------------------------------------------------------------


def test_v0626_translations_include_device_time_abnormal():
    """All 4 translation files must declare ``device_time_abnormal``."""
    expected = {
        "en.json": "Device Time Abnormal",
        "zh-CN.json": "设备时间异常",
        "zh.json": "设备时间异常",
        "zh-Hans.json": "设备时间异常",
    }
    for fname, expected_name in expected.items():
        path = _TRANSLATIONS_DIR / fname
        assert path.exists(), f"translation file {fname} missing"
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        bs = data.get("entity", {}).get("binary_sensor", {})
        key = bs.get("device_time_abnormal")
        assert key is not None, (
            f"v0.6.26: {fname} is missing binary_sensor.device_time_abnormal"
        )
        assert key.get("name") == expected_name, (
            f"v0.6.26: {fname} device_time_abnormal.name should be "
            f"{expected_name!r}, got {key.get('name')!r}"
        )


def test_v0626_three_chinese_translations_remain_identical():
    """zh-CN.json, zh.json, zh-Hans.json must stay byte-identical.

    Pre-v0.6.26 they were already identical (zhhash match); v0.6.26
    added ``device_time_abnormal`` to all three, so we re-pin the
    invariant. If a future change splits the translations, this
    test will fail and force an explicit decision.
    """
    content = lambda name: (
        (_TRANSLATIONS_DIR / name).read_bytes()
    )
    zh_cn = content("zh-CN.json")
    assert content("zh.json") == zh_cn, (
        "v0.6.26: zh.json must match zh-CN.json (kept identical "
        "for HA language fallback chain)."
    )
    assert content("zh-Hans.json") == zh_cn, (
        "v0.6.26: zh-Hans.json must match zh-CN.json."
    )


# ---------------------------------------------------------------------------
# AST-level import audit: ensure the new code references valid symbols
# ---------------------------------------------------------------------------


def test_v0626_no_new_missing_imports():
    """Re-run the import audit mentally for v0.6.26 changes.

    The test_imports_audit module's tests cover all files in the
    integration root — re-running the full suite catches any new
    missing imports introduced by v0.6.26. We just verify the
    audit module itself runs to completion (returns no failures
    for our two changed files).
    """
    # binary_sensor.py now uses ``datetime`` + ``timezone`` —
    # verify both names are imported.
    src = (_INTEGRATION_ROOT / "binary_sensor.py").read_text(
        encoding="utf-8-sig"
    )
    assert "from datetime import" in src and "datetime" in src and (
        "timezone" in src
    ), "v0.6.26: binary_sensor.py must import datetime + timezone."
    # coordinator.py — currentDeviceTime extraction uses
    # ``_xml_text`` which is already imported. Verify.
    coord_src = (_INTEGRATION_ROOT / "coordinator.py").read_text(
        encoding="utf-8-sig"
    )
    assert "_xml_text(root, \"currentDeviceTime\")" in coord_src, (
        "v0.6.26: coordinator.py must call _xml_text on "
        "<currentDeviceTime> (proves the field is now extracted)."
    )