# Changelog

Versions use semantic versioning. GitHub Releases and this file describe the same release.

## [0.3.0] - 2026-10-26

First public HACS release, based on the previously deployed local 0.2.0 integration.

### Added

- HACS custom-repository metadata, Home Assistant manifest links and original brand icon.
- Installation, manual-to-HACS migration, Energy dashboard, proxy and troubleshooting documentation.
- MIT license, contributor instructions, issue templates and release automation.
- English and Slovak setup translations.
- CI for integration tests, Home Assistant hassfest and HACS repository validation.
- Reproducible manual-install ZIP containing only `custom_components/sse_ezona`.

### Existing functionality included

- Email/password sign-in and automatic previous-day SSE Excel export.
- Complete 15-minute profile validation, DST handling and hourly historical Energy statistics.
- Seven-day revision checks, outage catch-up, bounded retries and persistent private archives.
- Optional SSE-only CONNECT proxy, separate proxy authentication and no direct fallback.
- Reauthentication, status sensors and authenticated archive access for custom dashboards.

### Fixed

- Avoid a Home Assistant startup deadlock when replaying retained statistics while Recorder is waiting for bootstrap to finish. Statistics are queued during startup and their commit is awaited only after Home Assistant is running. Retained history still replays after a restart.

### Compatibility

Requires Home Assistant 2026.10.2 or newer. Config-entry version, domain and statistic IDs are unchanged, so existing manual installations can move to HACS without recreating their meter. No personal history, dashboard exports, credentials or VPN configuration is included.

## [0.2.0] - 2026-10-26

Local prototype; not previously published as a GitHub release.

- Added per-request proxy routing for a Slovakia VPN gateway.
- Added proxy settings, endpoint validation and fail-closed network behavior.

## [0.1.0] - 2026-10-26

Local prototype; not previously published as a GitHub release.

- Initial daily SSE acquisition, profile validation, Recorder statistics, config flow and status entities.

[0.3.0]: https://github.com/Isiala/ha-sse-ezona/releases/tag/v0.3.0
