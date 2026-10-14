"""Red Hat offline token handling and SSO access-token exchange (spec FR-3)."""

from __future__ import annotations

import getpass
import logging
import os
import sys
import threading
import time
from typing import Callable

import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
CLIENT_ID = "rhsm-api"
TOKEN_GENERATE_URL = "https://access.redhat.com/management/api"
TOKEN_HOWTO_URL = "https://access.redhat.com/articles/3626371"
TOKEN_HELP = (
    f"Generate a Red Hat offline token at: {TOKEN_GENERATE_URL}\n"
    f"How-to guide for offline tokens: {TOKEN_HOWTO_URL}"
)
DEFAULT_TOKEN_ENV = "RH_OFFLINE_TOKEN"
DEFAULT_EXPIRES_IN = 900  # SSO access tokens last about 15 minutes
EXPIRY_SKEW = 60  # refresh this many seconds before expiry


class AuthError(Exception):
    """Authentication failed. The message always carries the token how-to links (FR-3.1)."""

    def __init__(self, message: str):
        super().__init__(f"{message}\n{TOKEN_HELP}")


def resolve_offline_token(
    env_name: str = DEFAULT_TOKEN_ENV,
    environ: dict | None = None,
    stdin=None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    """Return the offline token from the environment, else an interactive prompt (FR-3.2)."""
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin

    token = (environ.get(env_name) or "").strip()
    if token:
        logger.debug("Using offline token from $%s", env_name)
        return token

    if stdin is not None and stdin.isatty():
        token = prompt(
            f"A Red Hat offline token is required.\n{TOKEN_HELP}\n"
            f"(Tip: set ${env_name} to skip this prompt.)\n"
            "Red Hat offline token (input hidden): "
        ).strip()
        if token:
            return token
        raise AuthError("No offline token entered.")

    raise AuthError(f"No offline token found. Set ${env_name} or run interactively to be prompted.")


class TokenManager:
    """Exchanges the offline token for access tokens and refreshes them when needed (FR-3.3, FR-3.4)."""

    def __init__(
        self,
        offline_token: str,
        session: requests.Session | None = None,
        token_url: str = TOKEN_URL,
        timeout: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._offline_token = offline_token
        self._session = session or requests.Session()
        self._token_url = token_url
        self._timeout = timeout
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def access_token(self) -> str:
        with self._lock:
            if self._access_token is None or self._clock() >= self._expires_at - EXPIRY_SKEW:
                self._exchange()
            return self._access_token

    def invalidate(self) -> None:
        """Force a refresh on the next call, e.g. after a 401."""
        with self._lock:
            self._access_token = None

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token()}"}

    def _exchange(self) -> None:
        data = {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": self._offline_token}
        try:
            response = self._session.post(self._token_url, data=data, timeout=self._timeout)
        except requests.RequestException as exc:
            raise AuthError(f"Could not reach Red Hat SSO: {exc.__class__.__name__}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {}

        if "error" in body:
            description = body.get("error_description")
            message = f"Red Hat SSO rejected the offline token: {body['error']}"
            if description:
                message += f" ({description})"
            raise AuthError(message)
        if response.status_code != 200 or "access_token" not in body:
            raise AuthError(f"Could not retrieve an access token from Red Hat SSO (HTTP {response.status_code}).")

        self._access_token = body["access_token"]
        self._expires_at = self._clock() + float(body.get("expires_in") or DEFAULT_EXPIRES_IN)
        logger.info("Red Hat API access token retrieved")
