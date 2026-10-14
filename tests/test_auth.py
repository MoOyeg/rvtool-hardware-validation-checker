from urllib.parse import parse_qs

import pytest
import requests
import responses

from rvtools_hw_check.auth import (
    CLIENT_ID,
    TOKEN_GENERATE_URL,
    TOKEN_HOWTO_URL,
    TOKEN_URL,
    AuthError,
    TokenManager,
    resolve_offline_token,
)


class Tty:
    def __init__(self, tty: bool):
        self.tty = tty

    def isatty(self):
        return self.tty


def _fail_prompt(_text):
    raise AssertionError("prompt must not be shown when the env var is set")


def test_env_token_wins_and_prompt_is_not_shown():
    token = resolve_offline_token("RH_OFFLINE_TOKEN", environ={"RH_OFFLINE_TOKEN": " abc "}, stdin=Tty(True), prompt=_fail_prompt)
    assert token == "abc"


def test_custom_env_name():
    assert resolve_offline_token("MY_TOKEN", environ={"MY_TOKEN": "xyz"}, stdin=Tty(False)) == "xyz"


def test_prompt_shown_when_no_env_token_and_contains_links():
    shown = []

    def prompt(text):
        shown.append(text)
        return "typed-token"

    assert resolve_offline_token("RH_OFFLINE_TOKEN", environ={}, stdin=Tty(True), prompt=prompt) == "typed-token"
    assert len(shown) == 1
    assert TOKEN_GENERATE_URL in shown[0]
    assert TOKEN_HOWTO_URL in shown[0]


def test_empty_prompt_answer_is_an_error_with_links():
    with pytest.raises(AuthError) as err:
        resolve_offline_token("RH_OFFLINE_TOKEN", environ={}, stdin=Tty(True), prompt=lambda _t: "  ")
    assert TOKEN_HOWTO_URL in str(err.value)


def test_no_env_and_no_tty_is_an_error_with_links():
    with pytest.raises(AuthError) as err:
        resolve_offline_token("RH_OFFLINE_TOKEN", environ={}, stdin=Tty(False), prompt=_fail_prompt)
    message = str(err.value)
    assert "RH_OFFLINE_TOKEN" in message
    assert TOKEN_GENERATE_URL in message
    assert TOKEN_HOWTO_URL in message


@responses.activate
def test_exchange_uses_refresh_token_grant():
    responses.add(responses.POST, TOKEN_URL, json={"access_token": "access-1", "expires_in": 900})
    manager = TokenManager("offline-secret")
    assert manager.access_token() == "access-1"
    assert manager.auth_header() == {"Authorization": "Bearer access-1"}
    form = parse_qs(responses.calls[0].request.body)
    assert form == {"grant_type": ["refresh_token"], "client_id": [CLIENT_ID], "refresh_token": ["offline-secret"]}
    assert len(responses.calls) == 1  # reused, not exchanged per call


@responses.activate
def test_invalid_grant_reports_description_and_links_but_not_token():
    responses.add(
        responses.POST,
        TOKEN_URL,
        status=400,
        json={"error": "invalid_grant", "error_description": "Offline user session not found"},
    )
    with pytest.raises(AuthError) as err:
        TokenManager("offline-secret").access_token()
    message = str(err.value)
    assert "invalid_grant" in message
    assert "Offline user session not found" in message
    assert TOKEN_HOWTO_URL in message and TOKEN_GENERATE_URL in message
    assert "offline-secret" not in message


@responses.activate
def test_network_error_is_auth_error_with_links():
    responses.add(responses.POST, TOKEN_URL, body=requests.ConnectionError("boom"))
    with pytest.raises(AuthError) as err:
        TokenManager("offline-secret").access_token()
    assert "Could not reach Red Hat SSO" in str(err.value)
    assert TOKEN_HOWTO_URL in str(err.value)


@responses.activate
def test_missing_access_token_in_response():
    responses.add(responses.POST, TOKEN_URL, status=500, body="oops")
    with pytest.raises(AuthError, match="HTTP 500"):
        TokenManager("offline-secret").access_token()


@responses.activate
def test_refreshes_after_expiry_and_after_invalidate(clock):
    responses.add(responses.POST, TOKEN_URL, json={"access_token": "a1", "expires_in": 900})
    responses.add(responses.POST, TOKEN_URL, json={"access_token": "a2", "expires_in": 900})
    responses.add(responses.POST, TOKEN_URL, json={"access_token": "a3", "expires_in": 900})
    manager = TokenManager("offline", clock=clock)
    assert manager.access_token() == "a1"
    clock.now += 600
    assert manager.access_token() == "a1"
    clock.now += 300  # within the refresh skew of expiry
    assert manager.access_token() == "a2"
    manager.invalidate()
    assert manager.access_token() == "a3"
