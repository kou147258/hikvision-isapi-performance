"""Regression test for v0.6.24: coordinator.device_type must be initialized
in __init__, not only set inside _async_update_data().

User-reported after v0.6.23 (HACS auto-upgraded):

    Error while setting up hikvision_isapi_performance platform for sensor:
    'HikvisionISAPICoordinator' object has no attribute 'device_type'
        File "sensor.py", line 586, in async_setup_entry
        device_type = coordinator.device_type

Root cause:
- ``HikvisionISAPICoordinator.__init__`` initialized
  ``device_info`` / ``system_status`` / ``channels`` / ``capabilities``
  / etc. to empty containers so platforms can read them at
  setup time, but ``device_type`` was MISSING from that list.
- ``self.device_type = ...`` only happens inside
  ``_async_update_data()`` (after the first parse).
- Platforms' ``async_setup_entry`` runs in the same event loop
  turn as ``__init__.py``'s ``async_forward_entry_setups``,
  which is BEFORE the first refresh completes
  (``async_config_entry_first_refresh`` is scheduled as a
  background task).
- So ``sensor.async_setup_entry`` hit
  ``coordinator.device_type`` → ``AttributeError`` → the entire
  sensor platform failed to set up → only the binary_sensor and
  button entities survived (they don't read ``device_type``).

Fix:
- Add ``self.device_type: str = ""`` to ``__init__`` so it always
  exists. The actual normalized value still lands via
  ``self.device_type = normalize_device_type(...)`` inside
  ``_async_update_data``.

Pre-fix this test fails (the line ``self.device_type: str = ""``
is absent from ``__init__``). Post-fix it passes.
"""

from __future__ import annotations

from pathlib import Path


_COORD_SRC = Path(
    r"C:\Users\43457\Desktop\hikvision-isapi"
    r"\custom_components\hikvision_isapi_performance\coordinator.py"
)


def _coordinator_init_body() -> str:
    """Return the body of ``HikvisionISAPICoordinator.__init__``."""
    src = _COORD_SRC.read_text(encoding="utf-8-sig")
    # Find the class definition.
    cls_start = src.find("class HikvisionISAPICoordinator(")
    assert cls_start > 0, "HikvisionISAPICoordinator class not found"
    # Find __init__ inside the class.
    init_start = src.find("def __init__(", cls_start)
    assert init_start > 0, "HikvisionISAPICoordinator.__init__ not found"
    # Body runs until the next top-level def at column 4 inside the
    # class — i.e. until the next "    def " (4-space indented) or
    # "    @".
    rest = src[init_start:]
    end = None
    for marker in ("\n    def ", "\n    @", "\n    async def ", "\n    @"):
        pos = rest.find(marker, 1)
        if pos > 0 and (end is None or pos < end):
            end = pos
    if end is None:
        end = len(rest)
    return rest[:end]


def test_v0624_coordinator_init_has_device_type_default():
    """``HikvisionISAPICoordinator.__init__`` must set ``self.device_type``
    to a default value (empty string) so platforms can read it before
    the first refresh completes.
    """
    body = _coordinator_init_body()
    assert 'self.device_type: str = ""' in body, (
        "v0.6.24: HikvisionISAPICoordinator.__init__ must set "
        "``self.device_type: str = \"\"`` as a default. Pre-v0.6.24 the "
        "attribute was only assigned inside _async_update_data, so "
        "any platform reading ``coordinator.device_type`` during "
        "async_setup_entry (before first refresh) raised "
        "AttributeError → sensor platform failed to set up."
    )


def test_v0624_sensor_async_setup_entry_can_read_coordinator_device_type():
    """Cross-check: sensor.async_setup_entry reads coordinator.device_type
    AND the coordinator's __init__ provides a default for it. Together
    this means the sensor platform can set up before first refresh.
    """
    coord_body = _coordinator_init_body()
    sensor_src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\sensor.py"
    ).read_text(encoding="utf-8-sig")

    # sensor.py reads ``coordinator.device_type``.
    assert "device_type = coordinator.device_type" in sensor_src, (
        "Sanity: sensor.async_setup_entry must read coordinator.device_type"
    )
    # Coordinator's __init__ must default it.
    assert 'self.device_type: str = ""' in coord_body, (
        "v0.6.24: coordinator.__init__ must default device_type "
        "(otherwise sensor.py's read on a fresh coordinator raises "
        "AttributeError)."
    )