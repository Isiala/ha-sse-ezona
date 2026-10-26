"""Daily acquisition, persistent interval archive and hourly Recorder imports."""
from datetime import date, timedelta
import json
import logging
from pathlib import Path

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import CannotConnect, InvalidAuth, SSEClient, sse_ssl_context
from .const import (CONF_EIC, CONF_FETCH_HOUR, CONF_PROXY_URL, CONF_PROXY_USERNAME,
                    CONF_PROXY_PASSWORD, DEFAULT_FETCH_HOUR, DOMAIN, METER_TZ, SEED_FILE)
from .profiles import ProfileError, day_energy, due, fetch_range, hourly_statistics, merge_days, parse_export, revision

_LOGGER = logging.getLogger(__name__)


class SSECoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, config_entry=entry, name="SSE eZóna", update_interval=timedelta(minutes=30))
        self.entry = entry
        self.eic = entry.data[CONF_EIC]
        settings = {**entry.data, **entry.options}
        self.client = SSEClient(async_get_clientsession(hass),
                                proxy_url=settings.get(CONF_PROXY_URL, ""),
                                proxy_username=settings.get(CONF_PROXY_USERNAME, ""),
                                proxy_password=settings.get(CONF_PROXY_PASSWORD, ""))
        self.store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}", private=True)
        self.archive = {"eic": self.eic, "days": {}, "pending": [], "last_attempt": None,
                        "fetched_on": None, "last_success": None}
        self.statistic_id = f"{DOMAIN}:{self.eic.lower()}_consumption"

    async def async_load(self):
        self.client.ssl_context = await self.hass.async_add_executor_job(sse_ssl_context)
        saved = await self.store.async_load()
        if saved:
            if saved.get("eic") != self.eic:
                raise UpdateFailed("Stored data belongs to another meter")
            self.archive.update(saved)
        else:
            def load_seed():
                path = Path(self.hass.config.path(SEED_FILE))
                return json.loads(path.read_text()) if path.exists() else None
            try:
                seed = await self.hass.async_add_executor_job(load_seed)
                if seed and seed.get("eic") == self.eic:
                    self.archive["days"] = {day["date"]: day for day in seed["days"]}
            except (OSError, ValueError, KeyError, TypeError):
                raise UpdateFailed("The optional SSE seed file is invalid") from None
        try:
            checked, _ = merge_days({}, self.archive["days"])
            self.archive["days"] = checked
        except ProfileError:
            raise UpdateFailed("Stored SSE readings failed validation") from None
        # Replay once after restart; Recorder upserts by timestamp. This repairs
        # a shutdown between saving the interval archive and committing statistics.
        await self.async_publish_statistics(None)
        self.archive.pop("statistics_pending_from", None)
        await self.store.async_save(self.archive)

    async def async_publish_statistics(self, changed_from):
        if not self.archive["days"]:
            return
        rows = await self.hass.async_add_executor_job(hourly_statistics, self.archive["days"], changed_from)
        metadata = {"statistic_id": self.statistic_id, "source": DOMAIN,
                    "name": f"SSE {self.eic} electricity consumption", "unit_class": "energy",
                    "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                    "mean_type": StatisticMeanType.NONE, "has_sum": True}
        async_add_external_statistics(self.hass, metadata, rows)
        await get_instance(self.hass).async_block_till_done()

    def snapshot(self, now=None):
        now = now or dt_util.utcnow()
        yesterday = (now.astimezone(METER_TZ).date() - timedelta(days=1)).isoformat()
        days = self.archive["days"]
        return {"yesterday": yesterday, "yesterday_kwh": day_energy(days[yesterday]) if yesterday in days else None,
                "latest_day": max(days) if days else None, "profile_revision": revision(days),
                "pending": self.archive["pending"], "last_success": self.archive["last_success"],
                "status": "pending" if self.archive["pending"] else "complete" if yesterday in days else "waiting"}

    async def _async_update_data(self):
        now = dt_util.utcnow()
        if self.archive.get("statistics_pending_from"):
            await self.async_publish_statistics(self.archive["statistics_pending_from"])
            self.archive.pop("statistics_pending_from")
            await self.store.async_save(self.archive)
        hour = self.entry.options.get(CONF_FETCH_HOUR, self.entry.data.get(CONF_FETCH_HOUR, DEFAULT_FETCH_HOUR))
        if not due(now, hour, self.archive["fetched_on"], self.archive["pending"], self.archive["last_attempt"]):
            return self.snapshot(now)
        # Persist attempt time even on failure, so reloads do not hammer login.
        self.archive["last_attempt"] = now.timestamp()
        await self.store.async_save(self.archive)
        target = now.astimezone(METER_TZ).date() - timedelta(days=1)
        first, last = fetch_range(self.archive["days"], target, self.archive["pending"])
        # SSE exports are bounded to monthly chunks for long outage recovery.
        received = {}
        try:
            token = await self.client.login(self.entry.data[CONF_USERNAME], self.entry.data[CONF_PASSWORD])
            cursor = first
            while cursor <= last:
                end = min(last, cursor + timedelta(days=30))
                data = await self.client.export(self.eic, cursor, end, token)
                parsed = await self.hass.async_add_executor_job(parse_export, data, self.eic, cursor, end)
                received.update(parsed)
                cursor = end + timedelta(days=1)
            merged, changed_from = merge_days(self.archive["days"], received)
        except InvalidAuth:
            raise ConfigEntryAuthFailed("SSE login or meter access was rejected") from None
        except (CannotConnect, ProfileError) as exc:
            raise UpdateFailed(str(exc)) from None
        self.archive["days"] = merged
        if changed_from:
            self.archive["statistics_pending_from"] = changed_from
        self.archive["pending"] = [day.isoformat() for offset in range((last - first).days + 1)
                                   if (day := first + timedelta(days=offset)).isoformat() not in received]
        if not self.archive["pending"]:
            self.archive["fetched_on"] = now.astimezone(METER_TZ).date().isoformat()
            self.archive["last_success"] = now.isoformat()
        # Save first, then replay after restart if the Recorder transaction fails.
        await self.store.async_save(self.archive)
        if changed_from:
            await self.async_publish_statistics(changed_from)
            self.archive.pop("statistics_pending_from")
            await self.store.async_save(self.archive)
        return self.snapshot(now)


type SSEConfigEntry = ConfigEntry[SSECoordinator]
