"""Client for the Zwapgrid API.1 Consent and Accounting APIs (https://docs.zwapgrid.com/)."""

from __future__ import annotations

import uuid
from enum import IntEnum
from types import TracebackType
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import ZwapgridSettings


class ConsentStatus(IntEnum):
    CREATED = 0
    ACCEPTED = 1
    REVOKED = 2
    INACTIVE = 3


class ZwapgridError(RuntimeError):
    """Raised when the Zwapgrid API returns an error response."""


class ZwapgridClient:
    """Calls API.1 with an API key, adding a correlation ID to every request for traceability."""

    def __init__(self, settings: ZwapgridSettings, timeout: float = 30.0) -> None:
        self._settings = settings
        self._http = httpx.Client(timeout=timeout)

    def __enter__(self) -> ZwapgridClient:
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

    def list_consents(
        self, count: int = 10, current_page: int = 1, status: str | None = None
    ) -> dict[str, Any]:
        """List the consents (customer connections to accounting systems) for this API key."""
        params: dict[str, Any] = {"Count": count, "CurrentPage": current_page}
        if status:
            params["Status"] = status
        return self._get(
            f"{self._settings.consents_base_url}/api/v1/consents", params=params
        )

    def find_accepted_consent(self) -> dict[str, Any] | None:
        """Return the most recently created accepted consent, or None if there is none."""
        page = self.list_consents(count=100, status="ACCEPTED")
        accepted = [
            consent
            for consent in page.get("data") or []
            if consent.get("status") == ConsentStatus.ACCEPTED
        ]
        if not accepted:
            return None
        return max(accepted, key=lambda consent: consent.get("createdOn") or "")

    def get_consent(self, consent_id: str) -> dict[str, Any]:
        return self._get(f"{self._settings.consents_base_url}/api/v1/consents/{consent_id}")

    def get_company_information(self, consent_id: str) -> dict[str, Any]:
        """Fetch ERP company information for an accepted consent."""
        return self._get(
            f"{self._settings.accounting_base_url}/api/v1/consents/{consent_id}/companyinformation"
        )

    def get_supplier_invoices(self, consent_id: str) -> dict[str, Any]:
        """Fetch ERP company information for an accepted consent."""
        return self._get(
            f"{self._settings.accounting_base_url}/api/v1/consents/{consent_id}/supplierinvoices"
        )

    def create_supplier_invoice(
        self, consent_id: str, invoice: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a supplier invoice for an accepted consent.

        See https://docs.zwapgrid.com/api-guide/accounting-api-guide/supplier-invoices
        for the expected request body shape.
        """
        response = self._send(
            "POST",
            f"{self._settings.accounting_base_url}/api/v1/consents/{consent_id}/supplierinvoices",
            json=invoice,
        )
        return response.json()

    def create_consent(
        self, name: str, systems_settings: dict[str, str] | None = None
    ) -> str:
        """Create a consent and return its ID, which is only exposed via the Location header."""
        body: dict[str, Any] = {"name": name}
        if systems_settings:
            body["systemsSettings"] = systems_settings

        response = self._send(
            "POST", f"{self._settings.consents_base_url}/api/v1/consents", json=body
        )
        location = response.headers.get("Location", "")
        consent_id = location.rstrip("/").rsplit("/", 1)[-1]
        if not consent_id:
            raise ZwapgridError(
                "Consent was created but the response had no Location header to read the ID from."
            )
        return consent_id

    def create_one_time_code(self, consent_id: str) -> str:
        """Create a single-use code, valid for one hour, that unlocks the Onboarding Flow."""
        response = self._send(
            "POST", f"{self._settings.consents_base_url}/api/v1/consents/{consent_id}/otc"
        )
        code = response.json().get("code")
        if not code:
            raise ZwapgridError("One-time code response did not contain a code.")
        return code

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._send("GET", url, params=params).json()

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(
                method,
                url,
                headers={
                    "x-api-key": self._settings.api_key,
                    "x-correlation-id": str(uuid.uuid4()),
                    "Accept": "application/json",
                },
                **kwargs,
            )
        except httpx.HTTPError as error:
            raise ZwapgridError(f"Could not reach {url}: {error}") from error

        if not response.is_success:
            raise ZwapgridError(
                f"Zwapgrid returned {response.status_code} for {url}: {response.text.strip()}"
            )
        return response


def onboarding_url(
    consent_id: str,
    one_time_code: str,
    system: str | None = None,
    redirect_url: str | None = None,
    base_url: str = "https://onboarding.zwapgrid.com",
) -> str:
    """Build the Onboarding Flow URL. The one-time code must be URL-encoded."""
    path = f"{base_url}/consent/{consent_id}/"
    if system:
        path = f"{base_url}/consent/{consent_id}/{system}/"

    query = {"otc": one_time_code}
    if redirect_url:
        query["redirecturl"] = redirect_url
    return f"{path}?{urlencode(query)}"
