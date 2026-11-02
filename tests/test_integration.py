from copy import deepcopy
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

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
    await recorder.async_block_till_done()
    start = datetime.fromtimestamp(day["start"] - 3600, timezone.utc)
    end = datetime.fromtimestamp(day["start"] + 25 * 3600, timezone.utc)
    result = await recorder.async_add_executor_job(
        statistics_during_period, hass, start, end, {coordinator.statistic_id}, "hour", None, {"sum", "change"})
    rows = result[coordinator.statistic_id]
    assert len(rows) == 26  # one baseline plus 25 distinct UTC hours
    assert rows[-1]["sum"] == 100
    assert sum(row.get("change") or 0 for row in rows) == 100


@pytest.mark.asyncio
async def test_bootstrap_replay_does_not_wait_for_recorder_startup(hass):
    coordinator = coordinator_for(hass)
    coordinator.async_publish_statistics = SSECoordinator.async_publish_statistics.__get__(coordinator)
    day = make_day()
    coordinator.store.async_load = AsyncMock(return_value={**coordinator.archive, "days": {day["date"]: day}})
    recorder = MagicMock()
    startup_finished = asyncio.Event()
    recorder.async_block_till_done = AsyncMock(side_effect=startup_finished.wait)
    with patch.object(type(hass), "is_running", new_callable=PropertyMock, return_value=False), \
         patch("custom_components.sse_ezona.coordinator.get_instance", return_value=recorder), \
         patch("custom_components.sse_ezona.coordinator.async_add_external_statistics") as enqueue:
        await asyncio.wait_for(coordinator.async_load(), timeout=2)
    enqueue.assert_called_once()
    recorder.async_block_till_done.assert_not_awaited()
    assert coordinator.archive["days"] == {day["date"]: day}


@pytest.mark.asyncio
async def test_running_sync_still_waits_for_recorder_commit(hass):
    coordinator = coordinator_for(hass)
    coordinator.async_publish_statistics = SSECoordinator.async_publish_statistics.__get__(coordinator)
    day = make_day()
    coordinator.archive["days"] = {day["date"]: day}
    recorder = MagicMock()
    recorder.async_block_till_done = AsyncMock()
    with patch.object(type(hass), "is_running", new_callable=PropertyMock, return_value=True), \
         patch("custom_components.sse_ezona.coordinator.get_instance", return_value=recorder), \
         patch("custom_components.sse_ezona.coordinator.async_add_external_statistics") as enqueue:
        await coordinator.async_publish_statistics(None)
    enqueue.assert_called_once()
    recorder.async_block_till_done.assert_awaited_once()


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


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['login', 'export'])
async def test_transient_failure_stays_visible_across_cooldown_and_restart(hass, phase):
    from datetime import timedelta
    from custom_components.sse_ezona.api import CannotConnect

    coordinator = coordinator_for(hass)
    previous = make_day('2026-10-24')
    coordinator.archive['days'] = {previous['date']: previous}
    coordinator.archive['last_success'] = '2026-10-25T10:00:00+00:00'
    getattr(coordinator.client, phase).side_effect = CannotConnect('Unexpected login response')
    with patch('custom_components.sse_ezona.coordinator.dt_util.utcnow', return_value=NOW):
        data = await coordinator._async_update_data()
    assert data['status'] == f'retrying_{phase}'
    assert data['next_attempt'] == (NOW + timedelta(minutes=30)).isoformat()
    assert data['latest_day'] == previous['date']
    assert data['last_success'] == '2026-10-25T10:00:00+00:00'
    assert coordinator.archive['fetched_on'] is None
    assert coordinator.archive['last_error'] == phase
    assert 'Unexpected login response' not in str(coordinator.archive)
    coordinator.store.async_save.assert_awaited_with(coordinator.archive)

    restarted = coordinator_for(hass)
    restarted.store.async_load = AsyncMock(return_value=deepcopy(coordinator.archive))
    await restarted.async_load()
    with patch('custom_components.sse_ezona.coordinator.dt_util.utcnow', return_value=NOW + timedelta(minutes=29)):
        data = await restarted._async_update_data()
    assert data['status'] == f'retrying_{phase}'
    restarted.client.login.assert_not_awaited()

    days = {d['date']: d for d in [make_day(f'2026-10-{n}') for n in range(19, 26)]}
    with patch('custom_components.sse_ezona.coordinator.dt_util.utcnow', return_value=NOW + timedelta(minutes=30)), \
         patch('custom_components.sse_ezona.coordinator.parse_export', return_value=days):
        data = await restarted._async_update_data()
        await restarted._async_update_data()
    assert data['status'] == 'complete'
    assert data['last_error'] is None
    assert data['last_success'] == (NOW + timedelta(minutes=30)).isoformat()
    assert data['next_attempt'] == '2026-10-27T11:00:00+00:00'  # configured 12:00 local
    assert restarted.client.login.await_count == 1


@pytest.mark.asyncio
async def test_incomplete_success_clears_login_error_but_keeps_pending_retry(hass):
    from datetime import timedelta

    coordinator = coordinator_for(hass)
    coordinator.archive['last_error'] = 'login'
    day = make_day()
    with patch('custom_components.sse_ezona.coordinator.dt_util.utcnow', return_value=NOW), \
         patch('custom_components.sse_ezona.coordinator.parse_export', return_value={day['date']: day}):
        data = await coordinator._async_update_data()
    assert data['status'] == 'pending'
    assert data['last_error'] is None
    assert data['next_attempt'] == (NOW + timedelta(minutes=30)).isoformat()
    assert len(data['pending']) == 6


@pytest.mark.asyncio
async def test_reauth_is_distinct_from_transient_retry(hass):
    coordinator = coordinator_for(hass)
    coordinator.client.login.side_effect = InvalidAuth()
    with patch('custom_components.sse_ezona.coordinator.dt_util.utcnow', return_value=NOW):
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
    data = coordinator.snapshot(NOW)
    assert data['status'] == 'reauth_required'
    assert data['next_attempt'] is None


@pytest.mark.asyncio
async def test_sync_status_exposes_retry_schedule(hass):
    from datetime import timedelta
    from custom_components.sse_ezona.sensor import SSESensor

    coordinator = coordinator_for(hass)
    coordinator.archive.update(last_error='login', last_attempt=NOW.timestamp())
    coordinator.async_set_updated_data(coordinator.snapshot(NOW))
    sensor = SSESensor(coordinator, 'sync_status')
    assert sensor.native_value == 'retrying_login'
    attributes = sensor.extra_state_attributes
    assert attributes['last_attempt'] == NOW.isoformat()
    assert attributes['next_attempt'] == (NOW + timedelta(minutes=30)).isoformat()
    assert attributes['fetch_hour'] == 12
    assert 'test-password' not in str(attributes)


@pytest.mark.asyncio
async def test_real_refresh_keeps_retry_status_available_until_recovery(hass, freezer):
    from datetime import timedelta
    from custom_components.sse_ezona.api import CannotConnect
    from custom_components.sse_ezona.sensor import SSESensor

    freezer.move_to(NOW)
    coordinator = coordinator_for(hass)
    coordinator.client.login.side_effect = CannotConnect('Unexpected login response')
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    sensor = SSESensor(coordinator, 'sync_status')
    assert sensor.available
    assert sensor.native_value == 'retrying_login'
    freezer.move_to(NOW + timedelta(minutes=29))
    await coordinator.async_refresh()
    assert coordinator.client.login.await_count == 1
    assert sensor.native_value == 'retrying_login'
    assert sensor.available
    freezer.move_to(NOW + timedelta(minutes=30))
    coordinator.client.login.side_effect = None
    days = {d['date']: d for d in [make_day(f'2026-10-{n}') for n in range(19, 26)]}
    with patch('custom_components.sse_ezona.coordinator.parse_export', return_value=days):
        await coordinator.async_refresh()
    assert coordinator.client.login.await_count == 2
    assert sensor.available
    assert sensor.native_value == 'complete'


@pytest.mark.asyncio
async def test_fetch_hour_defaults_and_existing_options(hass):
    for data, options, expected in [
        ({k: v for k, v in DATA.items() if k != 'fetch_hour'}, {}, 6),
        (DATA, {}, 12),
        (DATA, {'fetch_hour': 6}, 6),
    ]:
        entry = MockConfigEntry(domain=DOMAIN, data=data, options=options)
        coordinator = SSECoordinator(hass, entry)
        assert coordinator.snapshot(NOW)['fetch_hour'] == expected


@pytest.mark.asyncio
async def test_manual_sensor_refresh_fetches_before_start_hour_and_coalesces_duplicates(hass, freezer):
    from datetime import timedelta
    from custom_components.sse_ezona.sensor import SSESensor

    # Automatic fetching waits for noon for this existing entry, but an explicit
    # update_entity should fetch immediately and still guard rapid duplicates.
    early = NOW.replace(hour=7)
    freezer.move_to(early)
    coordinator = coordinator_for(hass)
    coordinator.archive['last_attempt'] = (early - timedelta(minutes=2)).timestamp()
    days = {d['date']: d for d in [make_day(f'2026-10-{n}') for n in range(19, 26)]}
    with patch('custom_components.sse_ezona.coordinator.parse_export', return_value=days):
        await coordinator.async_refresh()
        coordinator.client.login.assert_not_awaited()
        await SSESensor(coordinator, 'sync_status').async_update()
        await SSESensor(coordinator, 'yesterday').async_update()
    assert coordinator.data['status'] == 'complete'
    assert coordinator.client.login.await_count == 1
    assert not coordinator._manual_fetch


@pytest.mark.asyncio
async def test_manual_failure_retains_retry_schedule_and_clears_manual_flag(hass, freezer):
    from datetime import timedelta
    from custom_components.sse_ezona.api import CannotConnect

    freezer.move_to(NOW)
    coordinator = coordinator_for(hass)
    coordinator.client.login.side_effect = CannotConnect('temporary failure')
    await coordinator.async_manual_refresh()
    assert coordinator.data['status'] == 'retrying_login'
    assert not coordinator._manual_fetch
    freezer.move_to(NOW + timedelta(seconds=29))
    await coordinator.async_manual_refresh()
    assert coordinator.client.login.await_count == 1
    freezer.move_to(NOW + timedelta(minutes=10))
    await coordinator.async_refresh()
    assert coordinator.client.login.await_count == 1
    assert coordinator.data['next_attempt'] == (NOW + timedelta(minutes=30)).isoformat()
