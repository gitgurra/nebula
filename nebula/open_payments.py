"""Client for the Open Payments API (https://docs.openpayments.io/)."""

from __future__ import annotations

import time
import uuid
from types import TracebackType
from typing import Any

import httpx

from .config import OpenPaymentsSettings

# Tokens are valid for one hour; refresh slightly early to avoid using an expiring token.
TOKEN_REFRESH_MARGIN_SECONDS = 60


class OpenPaymentsError(RuntimeError):
    """Raised when the Open Payments API returns an error response."""


class OpenPaymentsClient:
    """Authenticates with OAuth2 client credentials and calls the Open Payments REST API."""

    def __init__(self, settings: OpenPaymentsSettings, timeout: float = 30.0) -> None:
        self._settings = settings
        self._http = httpx.Client(timeout=timeout)
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def __enter__(self) -> OpenPaymentsClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def list_banks(self, iso_country_codes: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """List the ASPSPs (banks) available in the current environment."""
        codes = iso_country_codes if iso_country_codes is not None else self._settings.iso_country_codes
        params = {"isoCountryCodes": list(codes)} if codes else None
        payload = self._get("/psd2/aspspinformation/v1/aspsps", params=params)
        return payload.get("aspsps", [])

    def get_bank(self, bic_fi: str) -> dict[str, Any]:
        """Fetch details and capabilities for a single bank."""
        return self._get(f"/psd2/aspspinformation/v1/aspsps/{bic_fi}")

    def access_token(self) -> str:
        """Return a cached access token, requesting a new one when it is close to expiry."""
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token

        response = self._request(
            "POST",
            f"{self._settings.auth_base_url}/connect/token",
            data={
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "grant_type": "client_credentials",
                "scope": self._settings.scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        _raise_for_status(response, "requesting an access token")

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise OpenPaymentsError("Token response did not contain an access_token.")

        expires_in = float(payload.get("expires_in", 3600))
        self._access_token = token
        self._token_expires_at = time.monotonic() + max(expires_in - TOKEN_REFRESH_MARGIN_SECONDS, 0)
        return token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{self._settings.api_base_url}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "X-Request-ID": str(uuid.uuid4()),
                "Accept": "application/json",
            },
        )
        _raise_for_status(response, f"calling GET {path}")
        return response.json()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise OpenPaymentsError(f"Could not reach {url}: {error}") from error


def _raise_for_status(response: httpx.Response, action: str) -> None:
    if response.is_success:
        return
    raise OpenPaymentsError(
        f"Open Payments returned {response.status_code} while {action}: {response.text.strip()}"
    )
