from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.recorder import async_initialize_recorder
from homeassistant.setup import async_setup_component

from custom_components.sse_ezona import profiles as ws_profiles
from custom_components.sse_ezona.api import InvalidAuth
from custom_components.sse_ezona.const import DOMAIN
from custom_components.sse_ezona.coordinator import SSECoordinator

from .test_profiles import EIC, make_day

NOW = datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc)
DATA = {"username": "test@example.invalid", "password": "test-password", "eic": EIC, "fetch_hour": 12}


def coordinator_for(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=EIC)
    entry.add_to_hass(hass)
    coordinator = SSECoordinator(hass, entry)
    coordinator.store = MagicMock()
    coordinator.store.async_save = AsyncMock()
    coordinator.async_publish_statistics = AsyncMock()
    coordinator.client = MagicMock()
    coordinator.client.login = AsyncMock(return_value="test-token")
    coordinator.client.export = AsyncMock(return_value=b"fixture")
    entry.runtime_data = coordinator
    return coordinator


@pytest.mark.asyncio
async def test_daily_fetch_replay_and_pending_retry(hass):
    coordinator = coordinator_for(hass)
    day = make_day()
    with patch("custom_components.sse_ezona.coordinator.dt_util.utcnow", return_value=NOW), \
         patch("custom_components.sse_ezona.coordinator.parse_export", return_value={day["date"]: day}):
        data = await coordinator._async_update_data()
        assert data["yesterday_kwh"] == 100
        assert data["status"] == "pending"  # six earlier dates were not published
        assert len(data["pending"]) == 6
        assert coordinator.client.login.await_count == 1
        assert coordinator.async_publish_statistics.await_count == 1
        await coordinator._async_update_data()
        assert coordinator.client.login.await_count == 1
        assert coordinator.async_publish_statistics.await_count == 1


@pytest.mark.asyncio
async def test_restart_keeps_archive_and_republishes(hass):
    coordinator = coordinator_for(hass)
    day = make_day()
    saved = {**coordinator.archive, "days": {day["date"]: day}, "last_attempt": NOW.timestamp()}
    coordinator.store.async_load = AsyncMock(return_value=saved)
    await coordinator.async_load()
    assert coordinator.archive["days"] == saved["days"]
    coordinator.async_publish_statistics.assert_awaited_once_with(None)
    with patch("custom_components.sse_ezona.coordinator.dt_util.utcnow", return_value=NOW):
        await coordinator._async_update_data()
    coordinator.client.login.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_failure_preserves_readings_and_triggers_reauth(hass):
    coordinator = coordinator_for(hass)
    day = make_day("2026-10-24")
    coordinator.archive["days"] = {day["date"]: day}
    coordinator.client.login.side_effect = InvalidAuth()
    with patch("custom_components.sse_ezona.coordinator.dt_util.utcnow", return_value=NOW):
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
    assert coordinator.archive["days"] == {day["date"]: day}


@pytest.mark.asyncio
async def test_statistics_failure_is_replayed_without_another_login(hass):
    coordinator = coordinator_for(hass)
    day = make_day()
    coordinator.async_publish_statistics.side_effect = [RuntimeError("recorder unavailable"), None]
    with patch("custom_components.sse_ezona.coordinator.dt_util.utcnow", return_value=NOW), \
         patch("custom_components.sse_ezona.coordinator.parse_export", return_value={day["date"]: day}):
        with pytest.raises(RuntimeError):
            await coordinator._async_update_data()
        assert coordinator.archive["statistics_pending_from"] == day["date"]
        await coordinator._async_update_data()
        assert "statistics_pending_from" not in coordinator.archive
        assert coordinator.client.login.await_count == 1


@pytest.mark.asyncio
async def test_config_flow_and_duplicate_meter(hass, tmp_path):
    async_initialize_recorder(hass)
    assert await async_setup_component(hass, "recorder", {"recorder": {"db_url": f"sqlite:///{tmp_path / 'recorder.db'}"}})
    with patch("custom_components.sse_ezona.config_flow.SSEConfigFlow.validate_login", return_value=None), \
         patch("custom_components.sse_ezona.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER}, data=deepcopy(DATA))
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        duplicate = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER}, data=deepcopy(DATA))
        assert duplicate["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_dashboard_websocket_exposes_no_credentials(hass):
    coordinator = coordinator_for(hass)
    day = make_day()
    coordinator.archive["days"] = {day["date"]: day}
    connection = MagicMock()
    ws_profiles(hass, connection, {"id": 1, "eic": EIC})
    response = connection.send_result.call_args.args[1]
    assert response["days"] == [day]
    assert response["statistic_id"] == f"{DOMAIN}:{EIC.lower()}_consumption"
    assert "test-password" not in str(response)
    assert "test-token" not in str(response)


@pytest.mark.asyncio
async def test_real_recorder_hourly_import_and_replay(hass, tmp_path):
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    async_initialize_recorder(hass)
    assert await async_setup_component(hass, "recorder", {"recorder": {"db_url": f"sqlite:///{tmp_path / 'energy.db'}"}})
    coordinator = coordinator_for(hass)
    # This test uses the real publisher and SQLite Recorder, not a database mock.
    coordinator.async_publish_statistics = SSECoordinator.async_publish_statistics.__get__(coordinator)
    day = make_day()
    coordinator.archive["days"] = {day["date"]: day}
    await coordinator.async_publish_statistics(None)
    await coordinator.async_publish_statistics(None)
    recorder = get_instance(hass)
    start = datetime.fromtimestamp(day["start"] - 3600, timezone.utc)
    end = datetime.fromtimestamp(day["start"] + 25 * 3600, timezone.utc)
    result = await recorder.async_add_executor_job(
        statistics_during_period, hass, start, end, {coordinator.statistic_id}, "hour", None, {"sum", "change"})
    rows = result[coordinator.statistic_id]
    assert len(rows) == 26  # one baseline plus 25 distinct UTC hours
    assert rows[-1]["sum"] == 100
    assert sum(row.get("change") or 0 for row in rows) == 100


@pytest.mark.asyncio
async def test_full_integration_setup_sensor_statistics_and_unload(hass, tmp_path, freezer):
    from homeassistant.helpers import entity_registry as er

    freezer.move_to(NOW)
    async_initialize_recorder(hass)
    assert await async_setup_component(hass, "recorder", {"recorder": {"db_url": f"sqlite:///{tmp_path / 'full.db'}"}})
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=EIC)
    entry.add_to_hass(hass)
    days = {}
    for number in range(19, 26):
        day = make_day(f"2026-10-{number}")
        days[day["date"]] = day
    with patch("custom_components.sse_ezona.api.SSEClient.login", return_value="test-token"), \
         patch("custom_components.sse_ezona.api.SSEClient.export", return_value=b"fixture"), \
         patch("custom_components.sse_ezona.coordinator.parse_export", return_value=days):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    registry = er.async_get(hass)
    yesterday_id = registry.async_get_entity_id("sensor", DOMAIN, EIC + "_yesterday")
    state = hass.states.get(yesterday_id)
    assert float(state.state) == 100
    assert "state_class" not in state.attributes
    status_id = registry.async_get_entity_id("sensor", DOMAIN, EIC + "_sync_status")
    assert hass.states.get(status_id).state == "complete"
    assert entry.runtime_data.archive["fetched_on"] == "2026-10-26"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_reauth_updates_existing_entry_without_another_meter(hass, tmp_path):
    from homeassistant.config_entries import SOURCE_REAUTH

    async_initialize_recorder(hass)
    assert await async_setup_component(hass, "recorder", {"recorder": {"db_url": f"sqlite:///{tmp_path / 'reauth.db'}"}})
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=EIC)
    entry.add_to_hass(hass)
    with patch("custom_components.sse_ezona.config_flow.SSEConfigFlow.validate_login", return_value=None), \
         patch("homeassistant.config_entries.ConfigEntries.async_reload", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=DATA)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": DATA["username"], "password": "updated-test-password"})
        assert result["reason"] == "reauth_successful"
        assert entry.data["password"] == "updated-test-password"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.asyncio
async def test_proxy_config_validation_and_options_override(hass):
    from custom_components.sse_ezona.config_flow import SSEConfigFlow, check_proxy

    data = {**DATA, "proxy_url": "http://user:secret@vpn:8888"}
    flow = SSEConfigFlow()
    flow.hass = hass
    with patch("custom_components.sse_ezona.config_flow.SSEClient") as client:
        assert await flow.validate_login(data) == "invalid_proxy"
        client.assert_not_called()
    assert check_proxy({"proxy_password": "secret"}) == "invalid_proxy"
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, "proxy_url":"http://old-vpn:8888"},
                            options={"proxy_url":"http://new-vpn:8888"})
    coordinator = SSECoordinator(hass, entry)
    assert coordinator.client.proxy_url == "http://new-vpn:8888"


@pytest.mark.asyncio
async def test_setup_validates_login_and_export_through_proxy(hass):
    from custom_components.sse_ezona.config_flow import SSEConfigFlow

    flow = SSEConfigFlow()
    flow.hass = hass
    data = {**DATA, "proxy_url":"http://vpn:8888", "proxy_username":"sse-proxy", "proxy_password":"proxy-password"}
    with patch("custom_components.sse_ezona.config_flow.SSEClient") as cls:
        cls.return_value.login = AsyncMock(return_value="test-token")
        cls.return_value.export = AsyncMock()
        assert await flow.validate_login(data) is None
        assert cls.call_args.kwargs["proxy_url"] == data["proxy_url"]
        assert cls.call_args.kwargs["proxy_password"] == data["proxy_password"]
        cls.return_value.login.assert_awaited_once_with(DATA["username"], DATA["password"])
        cls.return_value.export.assert_awaited_once()
