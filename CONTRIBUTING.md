# Contributing

## Local development

Use Python 3.14 and a clean virtual environment:

```sh
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements_test.txt
python -m pytest -q
python scripts/build_release.py
```

Tests use synthetic meter data and mocked login/export requests, plus a real temporary SQLite Recorder database. They do not need an SSE account or network login. The pinned test plugin matches the minimum supported Home Assistant patch release. CI also checks the latest explicitly tested patch.

Keep the domain `sse_ezona`, config-entry version and external statistic IDs stable. A change to stored data needs an explicit migration and regression tests. Preserve complete-day validation, original UTC timestamps, revision idempotency, retry limits and fail-closed proxy routing.

Do not add captured exports, seed files, account IDs, credentials or VPN files as fixtures. Use synthetic data, including separate 92/96/100-quarter-hour local days. Review staged files before committing.

## Pull requests

Describe the user-visible change and validation. Run the tests; GitHub runs tests, hassfest and HACS validation. Update translations and CHANGELOG.md for user-facing changes. Add a regression test when it establishes a meaningful behavior, especially for parsing, date/time handling or Recorder imports.

## Publish a release

1. Update `custom_components/sse_ezona/manifest.json` with the new version.
2. Add a matching dated `## [version]` entry to CHANGELOG.md.
3. Merge the changes to `main` and wait for validation.
4. Run the **Release** workflow on `main` from GitHub Actions. It tests the committed code and runs hassfest before publishing `v<manifest version>` with notes extracted from the changelog and the manual-install ZIP.
5. Check the HACS validation triggered by the published release. HACS downloads component files from the release tag, not from the manual-install ZIP.

Actions have pinned revisions and Dependabot update checks. Workflow write permission is restricted to publishing the release. The CI workflow uses read-only repository permissions.

The manual-install archive has a `custom_components/sse_ezona/` root, so users can extract it into their HA config directory. `scripts/build_release.py --tag vX.Y.Z` verifies tag/version agreement and the changelog entry before building. The script also emits SHA256SUMS.txt. Never use a private setup ZIP as a release asset.
