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
    with patch("custom_components.sse_ezona.api.secrets.token_hex", return_value="expected-state"):
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


def response_session(body, content_type='application/json', status=200):
    class Content:
        async def iter_chunked(self, size):
            yield body

    response = MagicMock(status=status, headers={'Content-Type': content_type})
    response.content = Content()
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = MagicMock()
    session.request.return_value = context
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 403])
async def test_gateway_rejection_is_not_bad_credentials_and_does_not_leak_body(status):
    from custom_components.sse_ezona.api import GatewayRejected

    body = b'<html><title>Request Rejected</title>Support ID: private-marker</html>'
    session = response_session(body, 'text/html; charset=UTF-8', status)
    client = SSEClient(session, sse_ssl_context(), proxy_url='http://vpn:8888')
    with pytest.raises(GatewayRejected, match=f'login-start.*HTTP {status}') as error:
        await client.login('test@example.invalid', 'test-password')
    assert not isinstance(error.value, InvalidAuth)
    assert 'private-marker' not in str(error.value)
    assert '<html>' not in str(error.value)
    assert 'test-password' not in str(error.value)
    assert session.request.call_count == 1  # no authenticate request or direct fallback


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403])
async def test_api_authentication_rejection_still_requires_reauth(status):
    session = response_session(b'{"error":"unauthorized"}', status=status)
    client = SSEClient(session, sse_ssl_context())
    with pytest.raises(InvalidAuth):
        await client.login('test@example.invalid', 'test-password')


@pytest.mark.asyncio
async def test_export_gateway_error_never_includes_meter_or_token():
    from custom_components.sse_ezona.api import GatewayRejected
    from datetime import date

    session = response_session(b'<html>Request Rejected</html>', 'text/html')
    client = SSEClient(session, sse_ssl_context())
    with pytest.raises(GatewayRejected, match='profile export') as error:
        await client.export('24ZSS00000000000', date(2026, 10, 25), date(2026, 10, 25), 'test-token')
    assert '24ZSS00000000000' not in str(error.value)
    assert 'test-token' not in str(error.value)


@pytest.mark.asyncio
async def test_unexpected_header_is_not_echoed_and_login_stage_is_identified():
    session = response_session(b'<html>temporary maintenance</html>', 'text/private-header-marker')
    client = SSEClient(session, sse_ssl_context())
    with pytest.raises(CannotConnect, match='login-start.*expected JSON') as error:
        await client.login('test@example.invalid', 'test-password')
    assert 'private-header-marker' not in str(error.value)
    assert 'temporary maintenance' not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('content_type', ['Application/JSON; charset=UTF-8', 'application/problem+json'])
async def test_json_media_type_is_case_insensitive(content_type):
    session = response_session(b'{"synthetic":true}', content_type)
    client = SSEClient(session, sse_ssl_context())
    assert await client.post('/auth/api/public/login/v1/start', {}) == {'synthetic': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('body, message', [(b'not JSON', 'Unreadable authentication JSON'), (b'[]', 'expected an object')])
async def test_malformed_authentication_response_identifies_stage_without_body(body, message):
    client = SSEClient(response_session(body), sse_ssl_context())
    with pytest.raises(CannotConnect, match=message):
        await client.post('/auth/api/public/login/v1/authenticate', {})


@pytest.mark.asyncio
async def test_login_state_is_gateway_compatible_and_retains_192_bits():
    # These bytes make token_urlsafe produce hyphens; hex must encode the
    # same 24 random bytes without punctuation and verify the returned state.
    expected_state = 'fb' * 24
    client = SSEClient(None)
    start = {'temporaryAccessToken': 'temporary', 'nextStep': {
        'usernamePasswordMethod': {'secretPayload': 'challenge', 'passwordMethod': 'PLAIN'}}}
    redirect = {'redirectUri': '/#' + urlencode({'state': expected_state, 'token_type': 'Bearer',
                                              'access_token': 'test-token', 'expires_in': 7200})}
    client.post = AsyncMock(side_effect=[start, redirect])
    with patch('custom_components.sse_ezona.api.secrets.token_bytes', return_value=b'\xfb' * 24) as random_bytes:
        assert await client.login('test@example.invalid', 'test-password') == 'test-token'
    random_bytes.assert_called_once_with(24)
    assert client.post.call_args_list[0].args[1]['state'] == expected_state
