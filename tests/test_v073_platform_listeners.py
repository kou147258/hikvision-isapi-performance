"""v0.7.3: binary_sensor / camera / switch listeners were dead code.

Same defect as the two sensor-platform listeners fixed in
``test_v073_late_entities_and_nvr_channels.py``: the coordinator callback
was declared ``async def``.

Home Assistant's ``async_add_listener`` contract is ``Callable[[], None]``.
``async_update_listeners()`` calls each callback synchronously and
discards the return value, so an ``async def`` callback creates a
coroutine nobody awaits and its body never runs.

Why this matters to the user: ``__init__.py`` forwards platforms BEFORE
the first refresh completes, so at setup time ``coordinator.channels`` is
empty. The listeners were the ONLY path that could register these
entities, and every one of them was dead. Result on a fresh install:

  * no per-channel binary sensors (online / recording / motion)
  * no camera entities at all
  * no recording switches

The bodies contain no ``await``, so removing ``async`` is the whole fix.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import tests.conftest  # noqa: F401  installs homeassistant stubs

from custom_components.hikvision_isapi_performance.const import DOMAIN
from custom_components.hikvision_isapi_performance.coordinator import (
    HikvisionISAPIData,
)
from custom_components.hikvision_isapi_performance import (
    binary_sensor as binary_mod,
)
from custom_components.hikvision_isapi_performance import camera as camera_mod
from custom_components.hikvision_isapi_performance import switch as switch_mod


class _FakeCoordinator:
    """Reproduces HA's listener semantics exactly.

    ``fire()`` invokes callbacks synchronously and records any coroutine
    that comes back — a coroutine means the callback was ``async def`` and
    its body never executed.
    """

    def __init__(self, channels: list[dict[str, Any]] | None = None) -> None:
        self.channels = channels or []
        self.network_interfaces: list[dict[str, Any]] = []
        self.device_type = "networkvideorecorder"
        self.device_info: dict[str, Any] = {}
        self.data: HikvisionISAPIData | None = None
        self.capabilities: dict[str, Any] = {}
        self.streaming_channel_detail: dict[str, Any] = {}
        self.unique_id = f"{DOMAIN}_test"
        self._listeners: list[Any] = []
        self.unawaited_coroutines: list[Any] = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)

        def _remove():
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def fire(self) -> None:
        for listener in list(self._listeners):
            result = listener()
            if inspect.iscoroutine(result):
                self.unawaited_coroutines.append(result)
                result.close()


def _entry():
    e = type("E", (), {})()
    e.entry_id = "test_entry"
    e.data = {"host": "192.168.10.10"}
    e.options = {}
    return e


def _hass(coordinator):
    h = type("H", (), {})()
    h.data = {DOMAIN: {"test_entry": coordinator}}
    return h


# Real DS-7708N-I4 InputProxy channels (probe of 192.168.10.10).
LATE_CHANNELS = [
    {"id": "1", "name": "摄像机12", "online": True, "recording": True},
    {"id": "2", "name": "摄像机11", "online": True, "recording": False},
]

PLATFORMS = [
    ("binary_sensor", binary_mod),
    ("camera", camera_mod),
    ("switch", switch_mod),
]


@pytest.mark.parametrize("platform_name,platform_mod", PLATFORMS)
@pytest.mark.asyncio
async def test_registered_listener_is_not_a_coroutine_function(
    platform_name, platform_mod
):
    """The callback handed to async_add_listener must be plain sync."""
    coordinator = _FakeCoordinator()
    added: list[Any] = []

    await platform_mod.async_setup_entry(_hass(coordinator), _entry(), added.extend)

    assert coordinator._listeners, f"{platform_name} should register a listener"
    offenders = [
        getattr(fn, "__qualname__", repr(fn))
        for fn in coordinator._listeners
        if inspect.iscoroutinefunction(fn)
    ]
    assert not offenders, (
        f"{platform_name}: HA will never run the body of these async listeners: "
        f"{offenders}"
    )


@pytest.mark.asyncio
async def test_binary_sensor_registers_channel_entities_when_channels_arrive_late():
    """3 binary sensors per channel must appear after the refresh lands."""
    coordinator = _FakeCoordinator()
    added: list[Any] = []

    await binary_mod.async_setup_entry(_hass(coordinator), _entry(), added.extend)
    before = len(added)  # device-level sensors only

    coordinator.channels = LATE_CHANNELS
    coordinator.data = HikvisionISAPIData(
        device_info={"deviceType": "DVR"}, system_status={}, channels=LATE_CHANNELS,
        capabilities={}, storage={}, network_interfaces=[],
    )
    coordinator.fire()

    assert not coordinator.unawaited_coroutines
    new = added[before:]
    assert len(new) == 6, f"expected 3 binary sensors x 2 channels, got {len(new)}"


@pytest.mark.asyncio
async def test_camera_registers_entities_when_channels_arrive_late():
    """One camera entity per channel must appear after the refresh lands."""
    coordinator = _FakeCoordinator()
    added: list[Any] = []

    await camera_mod.async_setup_entry(_hass(coordinator), _entry(), added.extend)
    assert not added, "no channels at setup, so no cameras yet"

    coordinator.channels = LATE_CHANNELS
    coordinator.data = HikvisionISAPIData(
        device_info={"deviceType": "DVR"}, system_status={}, channels=LATE_CHANNELS,
        capabilities={}, storage={}, network_interfaces=[],
    )
    coordinator.fire()

    assert not coordinator.unawaited_coroutines
    assert len(added) == 2, f"expected 2 cameras, got {len(added)}"
    assert {c.channel_id for c in added} == {"1", "2"}


@pytest.mark.asyncio
async def test_switch_registers_entities_when_channels_arrive_late():
    """One recording switch per channel must appear after the refresh."""
    coordinator = _FakeCoordinator()
    added: list[Any] = []

    await switch_mod.async_setup_entry(_hass(coordinator), _entry(), added.extend)
    assert not added, "no channels at setup, so no switches yet"

    coordinator.channels = LATE_CHANNELS
    coordinator.data = HikvisionISAPIData(
        device_info={"deviceType": "DVR"}, system_status={}, channels=LATE_CHANNELS,
        capabilities={}, storage={}, network_interfaces=[],
    )
    coordinator.fire()

    assert not coordinator.unawaited_coroutines
    assert len(added) == 2, f"expected 2 switches, got {len(added)}"


@pytest.mark.asyncio
async def test_listeners_are_one_shot_and_do_not_duplicate_entities():
    """Firing twice must not register the same entities again."""
    coordinator = _FakeCoordinator()
    added: list[Any] = []

    await switch_mod.async_setup_entry(_hass(coordinator), _entry(), added.extend)

    coordinator.channels = LATE_CHANNELS
    coordinator.data = HikvisionISAPIData(
        device_info={}, system_status={}, channels=LATE_CHANNELS, capabilities={},
        storage={}, network_interfaces={},
    )
    coordinator.fire()
    first_count = len(added)
    # Guard against a vacuous pass: if the listener were still dead,
    # first_count would be 0 and the "no duplicates" assertion below
    # would hold trivially (0 == 0). Require the entities to exist first.
    assert first_count == 2, f"first fire should add 2 switches, got {first_count}"

    coordinator.fire()
    assert len(added) == first_count, "listener must be one-shot, not duplicating"
