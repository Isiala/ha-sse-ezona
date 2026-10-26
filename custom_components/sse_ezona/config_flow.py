"""UI setup and reauthentication for SSE email/password accounts."""
from datetime import timedelta
import re

import probatio as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import CannotConnect, ExtraAuthStep, InvalidAuth, SSEClient, sse_ssl_context, validate_proxy_url
from .const import (CONF_EIC, CONF_FETCH_HOUR, CONF_PROXY_URL, CONF_PROXY_USERNAME,
                    CONF_PROXY_PASSWORD, DEFAULT_FETCH_HOUR, DOMAIN, METER_TZ)


def proxy_schema(data):
    return {
        vol.Optional(CONF_PROXY_URL, default=data.get(CONF_PROXY_URL, "")): str,
        vol.Optional(CONF_PROXY_USERNAME, default=data.get(CONF_PROXY_USERNAME, "")): str,
        vol.Optional(CONF_PROXY_PASSWORD, default=data.get(CONF_PROXY_PASSWORD, "")):
            selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
    }


def check_proxy(data):
    try:
        data[CONF_PROXY_URL] = validate_proxy_url(data.get(CONF_PROXY_URL, ""))
        if (data.get(CONF_PROXY_USERNAME) or data.get(CONF_PROXY_PASSWORD)) and not data[CONF_PROXY_URL]:
            return "invalid_proxy"
        if ":" in data.get(CONF_PROXY_USERNAME, ""):
            return "invalid_proxy"
    except ValueError:
        return "invalid_proxy"
    return None


class SSEConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            user_input[CONF_EIC] = user_input[CONF_EIC].strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{16}", user_input[CONF_EIC]):
                errors[CONF_EIC] = "invalid_eic"
            else:
                await self.async_set_unique_id(user_input[CONF_EIC])
                self._abort_if_unique_id_configured()
                error = await self.validate_login(user_input)
                if error:
                    errors["base"] = error
                else:
                    return self.async_create_entry(title=f"SSE {user_input[CONF_EIC]}", data=user_input)
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
            vol.Required(CONF_EIC): str,
            vol.Optional(CONF_FETCH_HOUR, default=DEFAULT_FETCH_HOUR): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
            **proxy_schema(user_input or {}),
        }), errors=errors)

    async def validate_login(self, data):
        if error := check_proxy(data):
            return error
        context = await self.hass.async_add_executor_job(sse_ssl_context)
        client = SSEClient(async_get_clientsession(self.hass), context,
                           proxy_url=data.get(CONF_PROXY_URL, ""),
                           proxy_username=data.get(CONF_PROXY_USERNAME, ""),
                           proxy_password=data.get(CONF_PROXY_PASSWORD, ""))
        try:
            token = await client.login(data[CONF_USERNAME], data[CONF_PASSWORD])
            yesterday = dt_util.utcnow().astimezone(METER_TZ).date() - timedelta(days=1)
            await client.export(data[CONF_EIC], yesterday, yesterday, token)
        except ExtraAuthStep:
            return "extra_auth_step"
        except InvalidAuth:
            return "invalid_auth"
        except CannotConnect:
            return "cannot_connect"
        return None

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            merged = {**entry.data, **entry.options, **user_input}
            error = await self.validate_login(merged)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({
            vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
            vol.Required(CONF_PASSWORD): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        }), errors=errors)

    @staticmethod
    def async_get_options_flow(config_entry):
        return SSEOptionsFlow()


class SSEOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            if error := check_proxy(user_input):
                errors["base"] = error
            else:
                return self.async_create_entry(title="", data=user_input)
        hour = self.config_entry.options.get(CONF_FETCH_HOUR, self.config_entry.data.get(CONF_FETCH_HOUR, DEFAULT_FETCH_HOUR))
        settings = {**self.config_entry.data, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_FETCH_HOUR, default=hour): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
            **proxy_schema(settings),
        }), errors=errors)
