"""Regression tests for v0.6.29.

User feedback after v0.6.28 noted that the v0.6.25 KB/MB
heuristic is a *band-aid*, not a fix — obscure / new firmware
can still produce memory numbers that look wrong. Users wanted
visibility on suspect devices without having to grep
coordinator logs.

v0.6.29 surfaces the diagnostic as a HA binary sensor:
``mem_calibration_warn`` (BinarySensorDeviceClass.PROBLEM).
ON when ``_memory_calibration_anomalous(used, avail)`` returns
True — i.e. the post-v0.6.25 normalised MB values still look
physically implausible.

Crucially this is a *diagnostic*, not a fix: we don't auto-
recalibrate on top of the existing v0.6.25 heuristic. A second
heuristic could itself be wrong on a different firmware
generation, so we flag and let the user inspect.

Tests:
- ``_memory_calibration_anomalous`` returns True for KB/MB
  mis-calibration that survived v0.6.25 (very large avail)
- Returns True for inverted ratio (used > 10x available)
- Returns True for absolute garbage (> 8 GiB)
- Returns False for plausible Hikvision values
- Returns None for missing / zero data (don't false-alarm)
- Returns None for unparseable strings
- ``HikvisionISAPIMemCalibrationWarnBinarySensor`` is
  registered in async_setup_entry with translation_key +
  unique_id matching the contract
- All 4 translation files declare ``mem_calibration_warn``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


_REPO_ROOT = Path(r"C:\Users\43457\Desktop\hikvision-isapi")
_INTEGRATION_ROOT = (
    _REPO_ROOT / "custom_components" / "hikvision_isapi_performance"
)
_TRANSLATIONS_DIR = _INTEGRATION_ROOT / "translations"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _source_load_function(path: Path, fn_name: str, extra_ns: dict | None = None):
    body_lines = _read(path).splitlines()
    start_idx = None
    for i, line in enumerate(body_lines):
        if line.startswith(f"def {fn_name}("):
            start_idx = i
            break
    assert start_idx is not None, f"{fn_name} not found in {path}"
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
    if extra_ns:
        ns.update(extra_ns)
    exec(func_src, ns)
    return ns[fn_name]


# ---------------------------------------------------------------------------
# _memory_calibration_anomalous helper
# ---------------------------------------------------------------------------


def test_v0629_kb_mb_miscalibration_detected():
    """Very large ``memoryAvailable`` vs ``memoryUsage`` → True.

    Real-world scenario: some obscure firmware returns
    ``memoryAvailable`` in bytes (not KB, not MB). If the
    v0.6.25 50x heuristic missed it (e.g. memoryUsage is
    already huge because it's in bytes too), the values
    would be huge but roughly equal. If only one is in bytes
    and the other in MB, the ratio blows past 50.
    """
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(bs_path, "_memory_calibration_anomalous")
    # 100 MB used, 60 GB "available" → ratio 600x → anomalous
    assert fn("100", "60000") is True
    # 1 MB used, 100 MB available → ratio 100x → anomalous
    assert fn("1", "100") is True


def test_v0629_inverted_ratio_detected():
    """Used > 10x available is also anomalous (kernel would OOM)."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(bs_path, "_memory_calibration_anomalous")
    # 1000 MB used, 50 MB available → ratio 0.05x → anomalous
    assert fn("1000", "50") is True


def test_v0629_absolute_garbage_detected():
    """Values > 8 GiB are physically impossible on Hikvision."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(bs_path, "_memory_calibration_anomalous")
    # 10 GiB used → impossible
    assert fn("10240", "8192") is True
    # 100 GiB available → impossible
    assert fn("1024", "102400") is True


def test_v0629_plausible_values_not_flagged():
    """Real Hikvision values pass through as False."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(bs_path, "_memory_calibration_anomalous")
    # V4 NVR: 728 MB used, 403 MB available (ratio 0.55x)
    assert fn("728", "403") is False
    # V5 IPC: 61 MB used, 219 MB available (ratio 3.6x)
    assert fn("61", "219") is False
    # 4 GB used, 4 GB available (exactly 1.0x — typical NVR)
    assert fn("4096", "4096") is False
    # Used much smaller than available but within range
    assert fn("100", "1000") is False


def test_v0629_missing_data_returns_none():
    """Missing or zero data → None (unknown), don't false-alarm."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(bs_path, "_memory_calibration_anomalous")
    assert fn(None, None) is None
    assert fn(None, "100") is None
    assert fn("100", None) is None
    # Both zero (mid-restart scenario)
    assert fn("0", "0") is None
    # Only one populated — can't judge ratio
    assert fn("0", "100") is None
    assert fn("100", "0") is None


def test_v0629_unparseable_strings_return_none():
    """Garbage strings from a buggy firmware → None, not crash."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    fn = _source_load_function(bs_path, "_memory_calibration_anomalous")
    assert fn("not-a-number", "100") is None
    assert fn("100", "") is None
    assert fn("", "") is None
    assert fn("abc", "xyz") is None


# ---------------------------------------------------------------------------
# Binary sensor class
# ---------------------------------------------------------------------------


def test_v0629_mem_calibration_warn_binary_sensor_class():
    """``HikvisionISAPIMemCalibrationWarnBinarySensor`` exists.

    v0.6.29: surfaces memory-calibration diagnostics on the device
    card. PROBLEM device class so HA renders it red.
    """
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    src = _read(bs_path)
    assert (
        "class HikvisionISAPIMemCalibrationWarnBinarySensor(" in src
    ), (
        "v0.6.29: HikvisionISAPIMemCalibrationWarnBinarySensor "
        "class must exist in binary_sensor.py"
    )
    # async_setup_entry must instantiate the new class.
    assert (
        "HikvisionISAPIMemCalibrationWarnBinarySensor(coordinator, entry)" in src
    ), (
        "v0.6.29: async_setup_entry must instantiate the new "
        "binary sensor (otherwise the entity never registers)."
    )
    # unique_id uses the contract suffix.
    assert '_mem_calibration_warn' in src, (
        "v0.6.29: binary sensor unique_id must use "
        "_mem_calibration_warn as the suffix."
    )
    # Translation key matches the contract.
    assert (
        '_attr_translation_key = "mem_calibration_warn"' in src
    ), (
        "v0.6.29: binary sensor translation_key must be "
        "'mem_calibration_warn'."
    )
    # Device class PROBLEM (so it shows red on the device card).
    assert "BinarySensorDeviceClass.PROBLEM" in src, (
        "v0.6.29: binary sensor must use PROBLEM device class."
    )


def test_v0629_mem_calibration_warn_no_automatic_recalibration():
    """We surface diagnostics; we don't auto-recalibrate.

    The function must be a pure read — no mutation of
    ``system_status`` or any side-effects. Pins the design
    decision called out in the function's docstring.
    """
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    src = _read(bs_path)
    # Find the function body.
    body_lines = src.splitlines()
    start_idx = None
    for i, line in enumerate(body_lines):
        if line.startswith("def _memory_calibration_anomalous("):
            start_idx = i
            break
    assert start_idx is not None
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
    body = "\n".join(body_lines[start_idx:end_idx])
    # No writes (no `=`, no `del`, no `append` etc.)
    assert "self." not in body, (
        "v0.6.29: _memory_calibration_anomalous must not mutate "
        "any state (no self.*). It's a pure diagnostic read."
    )
    # No mutating operations on arguments either.
    assert ".append" not in body
    assert ".update" not in body
    assert "del " not in body


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------


def test_v0629_translations_have_mem_calibration_warn():
    """All 4 translation files declare ``mem_calibration_warn``."""
    expected_names = {
        "en.json": "Memory Calibration Suspect",
        "zh-CN.json": "内存换算疑似异常",
        "zh.json": "内存换算疑似异常",
        "zh-Hans.json": "内存换算疑似异常",
    }
    for fname, expected_name in expected_names.items():
        path = _TRANSLATIONS_DIR / fname
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        bs = data.get("entity", {}).get("binary_sensor", {})
        key = bs.get("mem_calibration_warn")
        assert key is not None, (
            f"v0.6.29: {fname} must declare "
            f"binary_sensor.mem_calibration_warn"
        )
        assert key.get("name") == expected_name, (
            f"v0.6.29: {fname} mem_calibration_warn.name should be "
            f"{expected_name!r}, got {key.get('name')!r}"
        )


def test_v0629_zh_translations_still_identical():
    """The 3 zh files stay byte-identical (HA language fallback chain)."""
    content = lambda name: (
        (_TRANSLATIONS_DIR / name).read_bytes()
    )
    zh_cn = content("zh-CN.json")
    assert content("zh.json") == zh_cn, (
        "v0.6.29: zh.json must match zh-CN.json."
    )
    assert content("zh-Hans.json") == zh_cn, (
        "v0.6.29: zh-Hans.json must match zh-CN.json."
    )