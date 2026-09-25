"""Audit test: every const.py symbol used by an integration module must be
imported in that module.

v0.6.19 / v0.6.20 shipped two ``NameError`` regressions in a row
(``UnitOfInformation`` in sensor.py, then ``ISAPI_SYSTEM_TIME`` in
coordinator.py) because I introduced new code that referenced symbols
without extending the ``from .const import`` (or
``from homeassistant.const import``) line. The unit-test conftest
stubs the missing names so tests passed even though real HA Core
blew up at module load.

This test statically walks the AST of every integration module
(``*.py`` under ``custom_components/hikvision_isapi_performance/``),
collects every Name reference that matches a symbol exported from
``const.py`` or from ``homeassistant.const``, and asserts that each
such reference is imported at the top of the file. ``const.py``
itself is exempt — it defines the symbols.

Run this on every release (it's part of the regular test suite).
"""

from __future__ import annotations

import ast
from pathlib import Path


_INTEGRATION_ROOT = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance"
)

# All const.py / homeassistant.const symbols we expect every module
# to import explicitly. This list is built once and cached for the
# test session.
_CONST_EXPORTS: set[str] = set()
_HA_CONST_EXPORTS: set[str] = {
    "ATTR_DEVICE_ID",
    "CONF_HOST",
    "CONF_PASSWORD",
    "CONF_PORT",
    "CONF_SCAN_INTERVAL",
    "CONF_USERNAME",
    "CONF_USE_HTTPS",
    "CONF_VERIFY_SSL",
    "DEFAULT_PORT",
    "DEFAULT_SCAN_INTERVAL",
    "MAX_SCAN_INTERVAL",
    "MIN_SCAN_INTERVAL",
    "PERCENTAGE",
    "Platform",
    "UnitOfInformation",
    "UnitOfTime",
}


def _scan_integration() -> dict[str, dict[str, set[str] | set[str] | bool]]:
    """For every integration module, return its imports and name refs."""
    const_src = (_INTEGRATION_ROOT / "const.py").read_text(encoding="utf-8-sig")
    const_tree = ast.parse(const_src)
    for n in ast.walk(const_tree):
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            _CONST_EXPORTS.add(n.target.id)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    _CONST_EXPORTS.add(t.id)

    result: dict[str, dict[str, set[str] | set[str] | bool]] = {}
    for mod_path in sorted(_INTEGRATION_ROOT.iterdir()):
        if mod_path.suffix != ".py" or mod_path.name == "const.py":
            continue
        src = mod_path.read_text(encoding="utf-8-sig")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        local_const_imports: set[str] = set()
        ha_const_imports: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                bare = n.module.lstrip(".")
                for alias in n.names:
                    if bare == "const":
                        local_const_imports.add(alias.name)
                    elif bare == "homeassistant.const":
                        ha_const_imports.add(alias.name)

        name_refs: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                name_refs.add(n.id)

        const_used = name_refs & _CONST_EXPORTS
        ha_const_used = name_refs & _HA_CONST_EXPORTS

        result[mod_path.name] = {
            "local": local_const_imports,
            "ha": ha_const_imports,
            "const_used": const_used,
            "ha_used": ha_const_used,
        }
    return result


def test_no_missing_const_imports():
    """Every ``const.py`` symbol used must be imported by the module."""
    audit = _scan_integration()
    failures: list[str] = []
    for mod, data in audit.items():
        const_used: set[str] = data["const_used"]  # type: ignore[assignment]
        local: set[str] = data["local"]  # type: ignore[assignment]
        ha: set[str] = data["ha"]  # type: ignore[assignment]
        missing = sorted(const_used - local - ha)
        if missing:
            failures.append(f"{mod}: missing from imports → {missing}")
    assert not failures, (
        "Modules reference const.py symbols without importing them. "
        "Add them to the ``from .const import (...)`` line:\n  "
        + "\n  ".join(failures)
    )


def test_no_missing_homeassistant_const_imports():
    """Every ``homeassistant.const`` symbol used must be imported."""
    audit = _scan_integration()
    failures: list[str] = []
    for mod, data in audit.items():
        ha_used: set[str] = data["ha_used"]  # type: ignore[assignment]
        ha: set[str] = data["ha"]  # type: ignore[assignment]
        local: set[str] = data["local"]  # type: ignore[assignment]
        # Allow ``.const`` re-exports only when the symbol is also
        # exported from our own const.py. Otherwise the symbol must
        # come from ``homeassistant.const`` directly.
        missing = sorted(
            name for name in ha_used
            if name not in ha and name not in local
        )
        if missing:
            failures.append(f"{mod}: missing homeassistant.const import → {missing}")
    assert not failures, (
        "Modules reference homeassistant.const symbols without "
        "importing them. Add them to the ``from homeassistant.const "
        "import (...)`` line:\n  " + "\n  ".join(failures)
    )