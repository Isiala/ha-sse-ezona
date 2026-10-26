"""SSE eZóna historical electricity integration."""
import probatio as vol

from homeassistant.components import websocket_api
from homeassistant.const import Platform
from homeassistant.core import callback

from .const import CONF_EIC, DOMAIN
from .coordinator import SSEConfigEntry, SSECoordinator

PLATFORMS = [Platform.SENSOR]


async def async_setup(hass, config):
    websocket_api.async_register_command(hass, profiles)
    return True


@websocket_api.websocket_command({vol.Required("type"): "sse_ezona/profiles", vol.Required("eic"): str})
@callback
def profiles(hass, connection, msg):
    """Serve interval history over HA's authenticated connection, never /local/."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data[CONF_EIC] == msg["eic"] and hasattr(entry, "runtime_data"):
            coordinator = entry.runtime_data
            connection.send_result(msg["id"], {
                "eic": coordinator.eic, "days": list(coordinator.archive["days"].values()),
                **coordinator.snapshot(), "statistic_id": coordinator.statistic_id,
            })
            return
    connection.send_error(msg["id"], "not_found", "This SSE meter is not loaded")


async def async_setup_entry(hass, entry: SSEConfigEntry):
    coordinator = SSECoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    # Missing/unpublished readings do not prevent creating the status entities.
    # Transient failures remain visible and the hourly coordinator retries later.
    await coordinator.async_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
