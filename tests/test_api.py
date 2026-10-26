from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode
import ssl

import pytest
import aiohttp

from custom_components.sse_ezona.api import CannotConnect, ExtraAuthStep, InvalidAuth, SSEClient, sse_ssl_context, validate_proxy_url


def test_tls_compatibility_retains_certificate_verification():
    context = sse_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.maximum_version == ssl.TLSVersion.TLSv1_2


@pytest.mark.asyncio
async def test_login_challenge_and_state_validation():
    client = SSEClient(None)
    start = {"temporaryAccessToken": "temporary", "nextStep": {
        "usernamePasswordMethod": {"secretPayload": "challenge", "passwordMethod": "PLAIN"}}}
    result = {"redirectUri": "/#" + urlencode({"state": "expected-state", "token_type": "Bearer",
                                               "access_token": "test-token", "expires_in": 7200})}
    with patch("custom_components.sse_ezona.api.secrets.token_urlsafe", return_value="expected-state"):
        client.post = AsyncMock(side_effect=[start, result])
        assert await client.login("test@example.invalid", "test-password") == "test-token"
    authenticate = client.post.call_args_list[1].args[1]
    assert authenticate == {"temporaryAccessToken": "temporary", "authToken": {"usernamePasswordToken": {
        "secretPayload": "challenge", "username": "test@example.invalid", "password": "test-password"}}}
    client.post = AsyncMock(side_effect=[start, result])
    with pytest.raises(InvalidAuth):
        await client.login("test@example.invalid", "test-password")


@pytest.mark.asyncio
async def test_interactive_challenge_is_not_automatically_completed():
    client = SSEClient(None)
    start = {"temporaryAccessToken": "temporary", "nextStep": {
        "usernamePasswordMethod": {"secretPayload": "challenge", "passwordMethod": "PLAIN"}}}
    client.post = AsyncMock(side_effect=[start, {"nextStep": {"emailMethod": {}}}])
    with pytest.raises(ExtraAuthStep):
        await client.login("test@example.invalid", "test-password")
    assert client.post.await_count == 2


@pytest.mark.parametrize("url", ["socks5://vpn:1080", "http://user:secret@vpn:8888",
    "http://vpn/path", "http://vpn?secret=value", "http://vpn#fragment",
    "http://vpn:99999", "http://", "http://vpn host:8888"])
def test_invalid_proxy_endpoint(url):
    with pytest.raises(ValueError):
        validate_proxy_url(url)


@pytest.mark.asyncio
async def test_proxy_failure_never_retries_direct_or_leaks_secrets():
    session = MagicMock()
    session.request.side_effect = aiohttp.ClientProxyConnectionError(
        MagicMock(), OSError("proxy-password must not appear"))
    client = SSEClient(session, sse_ssl_context(), proxy_url="http://vpn:8888/",
                       proxy_username="sse-proxy", proxy_password="proxy-password")
    with pytest.raises(CannotConnect, match="configured proxy") as error:
        await client.login("test@example.invalid", "sse-password")
    assert "password" not in str(error.value)
    assert session.request.call_count == 1
    args = session.request.call_args.kwargs
    assert args["proxy"] == "http://vpn:8888"
    assert args["proxy_headers"]["Proxy-Authorization"] == aiohttp.encode_basic_auth("sse-proxy", "proxy-password")
    assert args["ssl"].check_hostname


@pytest.mark.asyncio
async def test_export_uses_same_proxy_without_origin_proxy_credentials():
    class Content:
        async def iter_chunked(self, size):
            yield bytes.fromhex("d0cf11e0a1b11ae1")
    from datetime import date
    response = MagicMock(status=200, headers={"Content-Type":"application/vnd.ms-excel"})
    response.content = Content()
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = MagicMock()
    session.request.return_value = context
    client = SSEClient(session, sse_ssl_context(), proxy_url="http://vpn:8888",
                       proxy_username="sse-proxy", proxy_password="proxy-password")
    await client.export("24ZSS00000000000", date(2026,10,25), date(2026,10,25), "test-token")
    args = session.request.call_args.kwargs
    assert args["proxy"] == "http://vpn:8888"
    assert args["headers"]["Authorization"] == "Bearer test-token"
    assert "Proxy-Authorization" not in args["headers"]
