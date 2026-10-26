# Security and privacy

Report a suspected vulnerability privately through this repository's **Security → Report a vulnerability** facility when available. If it is unavailable, open an issue asking for a private contact channel without posting the vulnerability details.

Never post SSE passwords, bearer tokens, proxy credentials, VPN keys, Home Assistant tokens, configuration backups or raw consumption exports in a public issue. Redact EICs, account identifiers and response bodies. Logs should contain only the error category and timing needed to reproduce a problem.

The supported version is the latest published release. This community project cannot offer an SSE service-availability guarantee. The integration uses normal HTTPS certificate checks, keeps access tokens in memory and does not fall back to a direct route when an explicitly configured proxy fails.
