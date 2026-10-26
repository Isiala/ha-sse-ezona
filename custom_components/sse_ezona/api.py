"""Async client for the SSE website API. Never log response bodies or secrets."""
import asyncio
from datetime import date
import json
import secrets
import ssl
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

import aiohttp

from .const import BASE


class CannotConnect(Exception):
    """SSE could not be reached or returned an unexpected response."""


class InvalidAuth(Exception):
    """Credentials or token were rejected."""


class ExtraAuthStep(InvalidAuth):
    """The account requires an interactive login step."""


def validate_proxy_url(value):
    """Accept a CONNECT endpoint; keep credentials out of URLs and errors."""
    value = value.strip()
    if not value:
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None
                or url.path not in ("", "/") or url.query or url.fragment
                or any(char.isspace() for char in value)):
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Enter an HTTP proxy address without credentials or a path") from None
    return value.rstrip("/")


def sse_ssl_context():
    """SSE's gateway rejects Python 3.14's default TLS 1.3 requests.

    TLS 1.2 works against the live gateway. Retain CA and hostname verification;
    this is restricted to SSE requests and does not change HA's shared session.
    Call in an executor because loading the certificate store performs file IO.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


class SSEClient:
    def __init__(self, session: aiohttp.ClientSession, ssl_context=None, *,
                 proxy_url="", proxy_username="", proxy_password=""):
        self.session = session
        self.ssl_context = ssl_context
        self.proxy_url = validate_proxy_url(proxy_url) or None
        self.proxy_headers = ({"Proxy-Authorization": aiohttp.encode_basic_auth(proxy_username, proxy_password)}
                              if self.proxy_url and (proxy_username or proxy_password) else None)

    async def request(self, path, *, payload=None, token=None):
        if self.ssl_context is None:
            self.ssl_context = await asyncio.to_thread(sse_ssl_context)
        headers = {
            "Accept": "application/json;charset=UTF-8", "Accept-Language": "sk",
            "Content-Type": "application/json", "Origin": "https://ezona.sse.sk",
            "Referer": "https://ezona.sse.sk/", "Requestid": uuid.uuid4().hex,
            "User-Agent": "Mozilla/5.0",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            async with self.session.request(
                "POST" if payload is not None else "GET", BASE + path,
                json=payload, headers=headers, allow_redirects=False,
                ssl=self.ssl_context,
                proxy=self.proxy_url, proxy_headers=self.proxy_headers,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                if response.status in (401, 403):
                    raise InvalidAuth("SSE rejected authentication or meter access")
                if response.status != 200:
                    raise CannotConnect(f"SSE returned HTTP {response.status}")
                # Bound malformed responses and avoid holding an unbounded body.
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > 25_000_000:
                        raise CannotConnect("SSE response exceeded the size limit")
                return bytes(data), response.headers.get("Content-Type", "")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            message = "Could not reach SSE through the configured proxy" if self.proxy_url else "Could not reach SSE"
            raise CannotConnect(message) from None

    async def post(self, path, payload):
        body, content_type = await self.request(path, payload=payload)
        if "json" not in content_type:
            raise CannotConnect("Unexpected login response")
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise CannotConnect("Unreadable login response") from None
        if not isinstance(result, dict):
            raise CannotConnect("Unexpected login response")
        return result

    async def login(self, username, password):
        state = secrets.token_urlsafe(24)
        start = await self.post("/auth/api/public/login/v1/start", {
            "clientId": "sseWebPortal", "redirectUri": "/", "responseType": "token",
            "scope": "CHANNEL_WEB AUTHTYPE_* PERM_* ROLE_*", "state": state,
        })
        method = start.get("nextStep", {}).get("usernamePasswordMethod", {})
        if method.get("passwordMethod") != "PLAIN" or not method.get("secretPayload") or not start.get("temporaryAccessToken"):
            raise ExtraAuthStep("SSE does not offer the expected password login")
        result = await self.post("/auth/api/public/login/v1/authenticate", {
            "temporaryAccessToken": start["temporaryAccessToken"],
            "authToken": {"usernamePasswordToken": {
                "secretPayload": method["secretPayload"], "username": username, "password": password,
            }},
        })
        if result.get("nextStep"):
            raise ExtraAuthStep("An additional login step is required")
        try:
            redirect = urlsplit(result.get("redirectUri", ""))
            values = parse_qs(redirect.fragment)
            one = lambda key: values.get(key, [""])[0]
            if redirect.netloc and (redirect.scheme != "https" or redirect.netloc != "ezona.sse.sk"):
                raise ValueError
            if not secrets.compare_digest(one("state"), state):
                raise ValueError
            if one("token_type").lower() != "bearer" or not one("access_token") or int(one("expires_in")) <= 0:
                raise ValueError
            return one("access_token")
        except (ValueError, TypeError, AttributeError):
            raise InvalidAuth("Login did not return a valid bearer token") from None

    async def export(self, eic: str, first: date, last: date, token: str):
        query = urlencode({"periodFrom": first.isoformat(), "periodTo": last.isoformat(),
                           "sourceSystem": "OKTE", "losses": "true"})
        data, content_type = await self.request(
            f"/api/public/delivery-point/v1/ELECTRICITY_{eic}/consumption/profile-measurement/export?{query}", token=token)
        if "application/vnd.ms-excel" not in content_type or not data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            raise CannotConnect("SSE did not return an Excel workbook")
        return data
