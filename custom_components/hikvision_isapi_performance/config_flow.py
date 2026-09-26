"""Config flow for Hikvision ISAPI."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_USE_HTTPS,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DOMAIN,
    ISAPI_SYSTEM_DEVICE_INFO,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .isapi_client import (
    ISAPIAuthError,
    ISAPIConnectionError,
    ISAPIClient,
    ISAPIError,
)

_LOGGER = logging.getLogger(__name__)


def _default_use_https(port: int) -> bool:
    """Pick a sane HTTP/HTTPS default.

    v0.6.11: changed to always default to HTTP. Hikvision V5.x
    firmware serves ISAPI over plain HTTP by default on port 80.
    HTTPS on port 443 is opt-in via the device's web-server
    settings — the user has to explicitly enable it. Most HA
    users have HTTP, not HTTPS. Users with HTTPS-enabled firmware
    toggle the checkbox on.
    """
    return False


# Voluptuous schemas can't reference runtime-computed defaults, so
# the port-derived default is applied in async_step_user instead.
USER_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(
            int, vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_USERNAME, default="admin"): str,
        vol.Required(CONF_PASSWORD): str,
        # v0.6.11: both default to False (unchecked). HTTP is the
        # standard Hikvision ISAPI scheme; SSL verification is off
        # because most Hikvision devices use self-signed certs.
        vol.Optional(CONF_USE_HTTPS, default=False): BooleanSelector(),
        vol.Optional(CONF_VERIFY_SSL, default=False): BooleanSelector(),
    }
)


class HikvisionISAPIConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-facing config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: ask for host / port / username / password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input["host"]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            # v0.6.10: explicit HTTP/HTTPS selection. Default to the
            # port-based guess; user can override. Some Hikvision
            # firmware serves ISAPI over HTTP on port 443 (config
            # quirk) — port-based inference alone gets those wrong.
            use_https = user_input.get(
                CONF_USE_HTTPS, _default_use_https(port)
            )
            verify_ssl = user_input.get(CONF_VERIFY_SSL, False)

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured(updates=user_input)

            try:
                async with ISAPIClient(
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    verify_ssl=verify_ssl,
                    use_https=use_https,
                    timeout=DEFAULT_REQUEST_TIMEOUT,
                ) as client:
                    await client.get_text(ISAPI_SYSTEM_DEVICE_INFO)
            except ISAPIAuthError:
                errors["base"] = "invalid_auth"
            except ISAPIConnectionError:
                errors["base"] = "cannot_connect"
            except ISAPIError:
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error connecting to %s:%d", host, port
                )
                errors["base"] = "unknown"

            if not errors:
                return self.async_create_entry(
                    title=f"Hikvision {host}",
                    data={
                        "host": host,
                        CONF_PORT: port,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_USE_HTTPS: use_https,
                        CONF_VERIFY_SSL: verify_ssl,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "HikvisionISAPIOptionsFlow":
        return HikvisionISAPIOptionsFlow(config_entry)


class HikvisionISAPIOptionsFlow(OptionsFlow):
    """Handle the options flow (poll interval)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
            ),
        )

        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
