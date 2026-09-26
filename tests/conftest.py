"""Pytest config — stub homeassistant modules so unit tests can import the
integration without a full HA install.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import types

# Ensure the project root (where ``custom_components/`` lives) is on
# sys.path so the test file's absolute imports work. Pytest's default
# sys.path[0] is the tests/ directory; without this insert Python
# can't find ``custom_components.<integration_domain>``.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _install_stub(name: str, attrs: dict[str, object] | None = None) -> None:
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(module, k, v)
    sys.modules[name] = module


# Top-level homeassistant namespace
_install_stub("homeassistant")
# Parent sub-namespace so ``from homeassistant.helpers import X`` works
_install_stub("homeassistant.helpers")

# Core HA constants
_install_stub("homeassistant.const", {
    "CONF_HOST": "host",
    "CONF_PASSWORD": "password",
    "CONF_PORT": "port",
    "CONF_SCAN_INTERVAL": "scan_interval",
    "CONF_USERNAME": "username",
    "PERCENTAGE": "%",
    "Platform": types.SimpleNamespace(
        CAMERA="camera",
        SENSOR="sensor",
        BINARY_SENSOR="binary_sensor",
        SWITCH="switch",
        BUTTON="button",
    ),
    "UnitOfTime": types.SimpleNamespace(SECONDS="s", HOURS="h"),
    "UnitOfInformation": types.SimpleNamespace(MEGABYTES="MB", GIGABYTES="GB"),
    "ATTR_DEVICE_ID": "device_id",
})

# Core
_install_stub("homeassistant.core", {
    "HomeAssistant": object,
    "ServiceCall": object,
    "callback": lambda f: f,
})

# Config entries
class _StubConfigFlow:
    """Stub base class for ConfigFlow subclasses.

    Real HA ConfigFlow.__init_subclass__ accepts a `domain=` keyword
    argument. Our stub needs to accept arbitrary kwargs without
    forwarding them to object.__init_subclass__.
    """

    def __init_subclass__(cls, **kwargs):
        # Silently accept and discard kwargs (domain=, etc.) so
        # `class HikvisionISAPIConfigFlow(ConfigFlow, domain=DOMAIN):`
        # works in unit tests without a real HA install.
        pass


_install_stub("homeassistant.config_entries", {
    "ConfigEntry": object,
    "ConfigFlow": _StubConfigFlow,
    "ConfigFlowResult": dict,
    "OptionsFlow": type("OptionsFlow", (), {}),
})

# Components — camera
_install_stub("homeassistant.components.camera", {
    "Camera": type("Camera", (), {}),
})

# Components — sensor
# Real HA declares ``SensorEntityDescription`` as a frozen dataclass, and
# the integration subclasses it with ``@dataclass(frozen=True,
# kw_only=True)``. A plain-class stub broke that: the subclass's
# ``@dataclass`` could not inherit ``key`` / ``translation_key`` / etc.
# from a non-dataclass base, so importing sensor.py raised
# ``TypeError: __init__() got an unexpected keyword argument 'key'``.
# The old tests never hit this because they read sensor.py with regex
# instead of importing it.
@dataclasses.dataclass(frozen=True, kw_only=True)
class _SensorEntityDescriptionStub:
    key: str = ""
    name: str | None = None
    translation_key: str | None = None
    icon: str | None = None
    device_class: object = None
    state_class: object = None
    native_unit_of_measurement: object = None
    suggested_display_precision: int | None = None
    options: object = None
    entity_category: object = None
    has_entity_name: bool | None = None


_install_stub("homeassistant.components.sensor", {
    "SensorDeviceClass": types.SimpleNamespace(
        DURATION="duration",
        FREQUENCY="frequency",
        DATA_RATE="data_rate",
        DATA_SIZE="data_size",
        PERCENTAGE="percentage",
    ),
    "SensorEntity": type("SensorEntity", (), {}),
    "SensorEntityDescription": _SensorEntityDescriptionStub,
    "SensorStateClass": types.SimpleNamespace(
        MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing"
    ),
})

# Components — switch
_install_stub("homeassistant.components.switch", {
    "SwitchEntity": type("SwitchEntity", (), {}),
})

# Components — button
_install_stub("homeassistant.components.button", {
    "ButtonEntity": type("ButtonEntity", (), {}),
})

# Components — binary sensor
_install_stub("homeassistant.components.binary_sensor", {
    "BinarySensorDeviceClass": types.SimpleNamespace(
        CONNECTIVITY="connectivity",
        RUNNING="running",
        MOTION="motion",
        # v0.6.26: device_time_abnormal binary sensor uses PROBLEM
        # device class so HA renders it red on the device card.
        PROBLEM="problem",
    ),
    "BinarySensorEntity": type("BinarySensorEntity", (), {}),
})

# Exceptions
_install_stub("homeassistant.exceptions", {
    "ConfigEntryNotReady": Exception,
})

# Helpers — update coordinator
async def _stub_async_added_to_hass(self):
    """Default async_added_to_hass for stub CoordinatorEntity."""
    return None


_install_stub("homeassistant.helpers.update_coordinator", {
    "DataUpdateCoordinator": type(
        "DataUpdateCoordinator",
        (),
        {"__class_getitem__": classmethod(lambda cls, _x: cls)},
    ),
    "CoordinatorEntity": type(
        "CoordinatorEntity",
        (),
        {
            "__class_getitem__": classmethod(lambda cls, _x: cls),
            "__init__": lambda self, coordinator: setattr(self, "coordinator", coordinator),
            "async_added_to_hass": _stub_async_added_to_hass,
            "_handle_coordinator_update": lambda self: None,
        },
    ),
    "UpdateFailed": type("UpdateFailed", (Exception,), {}),
})

# Helpers — entity platform
_install_stub("homeassistant.helpers.entity_platform", {
    "AddEntitiesCallback": object,
})

# Helpers — aiohttp client
_install_stub("homeassistant.helpers.aiohttp_client", {
    "async_get_clientsession": lambda hass: None,
})

# Helpers — device registry
_install_stub("homeassistant.helpers.device_registry", {
    "async_get": lambda hass: types.SimpleNamespace(
        async_get=lambda device_id: None,
    ),
    # DeviceInfo is a TypedDict in real HA. The integration only reads
    # fields from the dict it builds, so a plain dict class is enough
    # for our unit tests.
    "DeviceInfo": dict,
})

# Helpers — config validation
_install_stub("homeassistant.helpers.config_validation", {
    "string": str,
})

# Helpers — selector
_ = sys.modules  # silence linter unused-import for sys
selector = types.ModuleType("homeassistant.helpers.selector")
selector.BooleanSelector = lambda: None
selector.NumberSelector = lambda *a, **k: None
selector.NumberSelectorConfig = lambda **k: None
selector.NumberSelectorMode = types.SimpleNamespace(BOX="box")
sys.modules["homeassistant.helpers.selector"] = selector


# ---- v0.6.12: pytest-asyncio fixture ----
# The httpx-based ISAPIClient tests need an event loop. Configure
# auto mode so we can write ``async def test_...`` without explicit
# ``@pytest.mark.asyncio`` decoration on each test.
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402


def pytest_collection_modifyitems(config, items):
    """Auto-mark async tests with asyncio mode."""
    for item in items:
        if isinstance(item, pytest.Function):
            if item.get_closest_marker("asyncio") is None:
                if hasattr(item, "obj") and hasattr(item.obj, "__code__"):
                    if item.obj.__code__.co_flags & 0x100:  # CO_COROUTINE
                        item.add_marker(pytest.mark.asyncio)


@pytest.fixture
def event_loop_policy():
    """Default policy; tests share the loop pytest-asyncio manages."""
    import asyncio as _asyncio
    return _asyncio.DefaultEventLoopPolicy()
