# SSE eZóna for Home Assistant

[![Validation](https://github.com/Isiala/ha-sse-ezona/actions/workflows/validate.yml/badge.svg)](https://github.com/Isiala/ha-sse-ezona/actions/workflows/validate.yml)
[![Release](https://img.shields.io/github/v/release/Isiala/ha-sse-ezona)](https://github.com/Isiala/ha-sse-ezona/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Import household electricity profiles from **Stredoslovenská energetika's eZóna** into Home Assistant. Fetch the previous local day automatically and display consumption on its original dates in the built-in **Energy dashboard**.

This is an independent community integration using SSE's undocumented website API. It is not affiliated with or supported by SSE. The API can change without notice.

## What it does

- Signs in with an SSE **email/username and password**; no browser or manually copied bearer token is needed for daily operation.
- Downloads the SSE Excel profile, validates complete 15-minute readings and converts average kW to kWh.
- Fetches daily after **12:00 Europe/Bratislava** by default, within the next 30-minute check. The hour is configurable.
- Rechecks seven days for corrections, catches up after an outage in monthly chunks, and retries unpublished days at most every three hours after the configured hour.
- Handles 23-, 24- and 25-hour local days. Missing readings are not replaced with zero.
- Imports hourly historical Recorder statistics, so yesterday's consumption is recorded against yesterday rather than today's download time.
- Exposes Yesterday energy, Sync status and Last successful sync sensors, plus an authenticated WebSocket endpoint for custom dashboards.
- Supports an optional HTTP CONNECT proxy for an SSE-only VPN route.

## Requirements

- **Home Assistant 2026.10.2 or newer** and Recorder enabled. Older releases are not supported by this version.
- An eZóna account with email/password sign-in and access to an **electricity profile export**. Check that the portal can export your meter's quarter-hour data manually first; not every meter/account provides it.
- Your electricity delivery point's **16-character EIC**, without the `ELECTRICITY_` prefix.
- Network access to SSE. If SSE restricts access from abroad, use an appropriate Slovak connection or the optional proxy below. A VPN subscription is not part of this integration.

## Install with HACS

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/Isiala/ha-sse-ezona` with type **Integration**.
3. Find **SSE eZóna** and download the latest release.
4. Restart **Home Assistant Core**.
5. Open **Settings → Devices & services → Add integration → SSE eZóna**.
6. Enter your SSE email/password, EIC and preferred fetch hour. Leave the proxy fields empty for a direct connection.

Setup verifies both sign-in and access to yesterday's Excel export. Enter credentials in Home Assistant; do not put them in GitHub issues.

This is a **HACS custom repository**, not a listing in HACS's default catalog.

### Existing manual installation

Back up your Home Assistant configuration first. Add this repository in HACS and download it over the existing `custom_components/sse_ezona` directory, then restart Core. **Keep the existing SSE config entry**: do not delete it or add the meter again.

The integration domain, config-entry version, entity unique IDs and external statistic IDs are unchanged from the local 0.1/0.2 prototypes. HACS manages the code only. Existing credentials, interval archives in `.storage`, Recorder history and an optional private `/config/sse_ezona_seed.json` remain outside the downloaded component. Existing dashboard cards are separate and are not installed or replaced by this repository.

### Manual installation

Download `ha-sse-ezona-<version>.zip` from [Releases](https://github.com/Isiala/ha-sse-ezona/releases), extract it into your Home Assistant **config** directory, and restart Core. The resulting path must be:

```text
/config/custom_components/sse_ezona/manifest.json
```

Do not extract the entire repository, tests or documentation into `custom_components`.

## Configure the Energy dashboard

Open **Energy → settings → Grid connections → Add grid connection**. For imported electricity choose:

```text
SSE <YOUR_EIC> electricity consumption
```

The underlying statistic ID is `sse_ezona:<your_eic_in_lowercase>_consumption`. Use that historical source, not the **Yesterday energy** diagnostic sensor, which deliberately has no state class.

SSE readings arrive for completed days. The Energy dashboard defaults to today, so select **yesterday or an earlier date** to see imported consumption. A new installation starts with the last seven days and retains subsequent complete profiles; it does not automatically download years of history.

Only grid consumption is imported. VT/NT tariff splits, prices, generation and energy exported to the grid are not supplied by this profile. This integration does not infer those values.

## Settings and entities

Change fetch time and proxy settings under **Settings → Devices & services → SSE eZóna → options**.

| Setting | Meaning |
| --- | --- |
| SSE email or username / password | Account used for each daily sign-in |
| EIC | One electricity delivery point; one config entry per EIC |
| Fetch hour | Local hour, 0–23, in Europe/Bratislava |
| Proxy URL | Optional `http://host:port` or `https://host:port` CONNECT endpoint, without a path or embedded credentials |
| Proxy username / password | Optional, separate credentials for that proxy |

| Entity | Meaning |
| --- | --- |
| Yesterday energy | Complete previous local day's kWh, or unknown if unavailable |
| Sync status | `complete`, `pending` or `waiting`; includes latest complete day and pending dates |
| Last successful sync | Time of a successful scheduled acquisition; initially unknown before the first run |

## Optional Slovakia VPN route

Run a VPN gateway separately, for example Gluetun configured with your provider's Slovak endpoint, and put its HTTP CONNECT proxy address in this integration's settings:

```text
Home Assistant SSE integration → private CONNECT proxy → Slovakia VPN → SSE
```

Only SSE requests use the proxy. A proxy failure stops the fetch; it does not fall back to the direct connection. SSE HTTPS still verifies its server certificate and hostname. This integration neither installs a VPN nor changes the host's default route, DNS, or other integrations.

On Home Assistant OS, use a gateway app's internal DNS hostname and avoid publishing its proxy port on the LAN or internet. VPN credentials belong in the gateway, not this integration. See [Gluetun's provider documentation](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers) for provider-specific configuration; a particular provider or community gateway app is not required.

## Data and privacy

Credentials are stored in Home Assistant's normal config entry for unattended use. This is **not encrypted credential storage**; protect your configuration and backups. Bearer tokens stay in memory. The code does not log passwords, tokens or API response bodies. No actual account identifiers, private consumption archive or VPN configuration is included in this repository or release ZIP.

Interval history is retained privately in `/config/.storage/sse_ezona_<entry_id>`. Recorder holds hourly external statistics. The optional seed path is `/config/sse_ezona_seed.json`; it is not required for normal use and is never distributed here.

Custom dashboards can request the archive over Home Assistant's authenticated WebSocket connection:

```json
{"id": 1, "type": "sse_ezona/profiles", "eic": "YOUR_16_CHAR_EIC"}
```

The response contains interval history and status, never credentials. Do not serve private readings from `/config/www` or publish a response in a bug report without redacting it.

## Troubleshooting

- **Could not connect:** check the portal manually, SSE access restrictions and the proxy connection. The gateway is contacted with TLS 1.2 because its current security gateway rejected default Python TLS requests during testing; CA and hostname checks remain enabled.
- **Invalid authentication:** re-enter credentials using Home Assistant's reauthentication flow. Extra email-code, CAPTCHA and social-login challenges are not automated.
- **Unknown yesterday / pending dates:** SSE may not have published a complete day. The integration waits and retries; it does not invent zero consumption.
- **Energy is blank today:** choose yesterday. Successful setup does not make SSE a live meter.
- **No Last successful sync yet:** setup validates the export, but the first scheduled acquisition occurs after the configured fetch hour.
- **Custom card error after a card update:** reload the browser or reset its frontend cache. HACS installation of this integration does not install a dashboard card.

Quarter-hour timestamps retain the current assumption that SSE labels mark interval **starts**; this has not been confirmed by SSE. `losses=true` and the OKTE source match the tested portal export. Revisions update cumulative statistics from the affected day onward. Only complete nonnegative consumption profiles are supported.

## Updates and removal

Read the [changelog](CHANGELOG.md) or [release notes](https://github.com/Isiala/ha-sse-ezona/releases) before updating through HACS, then restart Core. Removing a config entry does not erase imported Recorder statistics. Remove those separately with Home Assistant's statistics tools only if you intend to delete the history.

For development, tests and publishing see [CONTRIBUTING.md](CONTRIBUTING.md). Report reproducible problems in [Issues](https://github.com/Isiala/ha-sse-ezona/issues); follow [SECURITY.md](SECURITY.md) for sensitive reports.
