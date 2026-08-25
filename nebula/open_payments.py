"""Client for the Open Payments API (https://docs.openpayments.io/)."""

from __future__ import annotations

import time
import uuid
from datetime import date
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
        self._tokens: dict[str, tuple[str, float]] = {}

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

    def access_token(self, scope: str | None = None) -> str:
        """Return a cached access token for a scope, refreshing when close to expiry.

        Tokens are cached per scope because the account information, payment initiation and
        ASPSP information APIs each require their own scope on the token.
        """
        wanted = scope or self._settings.scope
        cached = self._tokens.get(wanted)
        if cached and time.monotonic() < cached[1]:
            return cached[0]

        response = self._request(
            "POST",
            f"{self._settings.auth_base_url}/connect/token",
            data={
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "grant_type": "client_credentials",
                "scope": wanted,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        _raise_for_status(response, f"requesting an access token for scope '{wanted}'")

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise OpenPaymentsError("Token response did not contain an access_token.")

        expires_in = float(payload.get("expires_in", 3600))
        expiry = time.monotonic() + max(expires_in - TOKEN_REFRESH_MARGIN_SECONDS, 0)
        self._tokens[wanted] = (token, expiry)
        return token

    def create_payment(self, payment_product: str, payment: dict[str, Any]) -> dict[str, Any]:
        """Initiate a single payment. Returns the paymentId and the authorisation links.

        The response may carry a `tppMessages` warning such as CREDITOR_ACCOUNT_FLAGGED when
        the creditor is on Svensk Handel's watchlist, which is worth surfacing rather than
        discarding.
        """
        return self._pis_request(
            "POST", f"/psd2/paymentinitiation/v1/payments/{payment_product}", json=payment
        )

    def create_payment_authorisation(
        self, payment_product: str, payment_id: str
    ) -> dict[str, Any]:
        """Start an authorisation process, returning its ID and the bank's SCA methods."""
        return self._pis_request(
            "POST", f"{_payment_path(payment_product, payment_id)}/authorisations"
        )

    def start_payment_authorisation(
        self,
        payment_product: str,
        payment_id: str,
        authorisation_id: str,
        authentication_method_id: str,
    ) -> dict[str, Any]:
        """Trigger the chosen SCA method, which prompts the PSU in their banking app."""
        return self._pis_request(
            "PUT",
            f"{_payment_path(payment_product, payment_id)}/authorisations/{authorisation_id}",
            json={"authenticationMethodId": authentication_method_id},
        )

    def get_payment_sca_status(
        self, payment_product: str, payment_id: str, authorisation_id: str
    ) -> dict[str, Any]:
        """Read the SCA status, which ends at 'finalised' or 'failed'."""
        return self._pis_request(
            "GET",
            f"{_payment_path(payment_product, payment_id)}/authorisations/{authorisation_id}",
        )

    def authorise_payment(
        self,
        payment_product: str,
        payment_id: str,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 180.0,
    ) -> str:
        """Run the decoupled SCA flow end to end, returning the final scaStatus.

        The PSU has to approve the payment in their banking app while this polls, so the
        timeout is generous. Returns 'finalised' on success.
        """
        created = self.create_payment_authorisation(payment_product, payment_id)
        authorisation_id = created.get("authorisationId")
        if not authorisation_id:
            raise OpenPaymentsError("Authorisation response did not contain an authorisationId.")

        method = _preferred_sca_method(created.get("scaMethods") or [])
        self.start_payment_authorisation(
            payment_product, payment_id, authorisation_id, method
        )

        deadline = time.monotonic() + timeout_seconds
        while True:
            status = self.get_payment_sca_status(
                payment_product, payment_id, authorisation_id
            ).get("scaStatus", "")
            if status in {"finalised", "failed"}:
                return status
            if time.monotonic() >= deadline:
                raise OpenPaymentsError(
                    f"Authorisation {authorisation_id} was still '{status}' after "
                    f"{timeout_seconds:.0f}s. Approve the payment in the banking app and retry."
                )
            time.sleep(poll_seconds)

    def get_payment_status(self, payment_product: str, payment_id: str) -> dict[str, Any]:
        """Poll the payment's transactionStatus. RJCT means the bank rejected it."""
        return self._pis_request(
            "GET",
            f"{_payment_path(payment_product, payment_id)}/status",
            headers={"X-Feature-Flags": "new-statuses-global"},
        )

    def _pis_request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call a payment initiation endpoint with the PSU context every bank requires."""
        self._settings.require_payment_settings()
        request_headers = {
            "Authorization": f"Bearer {self.access_token(self._settings.pis_scope)}",
            "X-Request-ID": str(uuid.uuid4()),
            "X-BicFi": self._settings.bicfi,
            "PSU-ID": self._settings.psu_id,
            "PSU-Corporate-ID": self._settings.psu_corporate_id,
            "PSU-IP-Address": self._settings.psu_ip_address,
            "PSU-User-Agent": self._settings.psu_user_agent,
            "TPP-Redirect-Preferred": "false",
            "Accept": "application/json",
            **(headers or {}),
        }
        response = self._request(
            method,
            f"{self._settings.api_base_url}{path}",
            json=json,
            headers=request_headers,
        )
        _raise_for_status(response, f"calling {method} {path}")
        return response.json() if response.content else {}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{self._settings.api_base_url}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {self.access_token(self._settings.scope)}",
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


def domestic_payment_payload(
    debtor_iban: str,
    creditor_iban: str,
    creditor_name: str,
    amount: float,
    currency: str = "SEK",
    execution_date: date | None = None,
    remittance_information: str = "",
) -> dict[str, Any]:
    """Build a payment body for an account-to-account transfer (payment-product 'domestic')."""
    return {
        "instructedAmount": {"amount": f"{amount:.2f}", "currency": currency},
        "debtorAccount": {"iban": debtor_iban, "currency": currency},
        "creditorAccount": {"iban": creditor_iban, "currency": currency},
        "creditorName": creditor_name,
        "requestedExecutionDate": (execution_date or date.today()).isoformat(),
        "remittanceInformationUnstructured": remittance_information,
    }


def swedish_giro_payment_payload(
    debtor_iban: str,
    giro_number: str,
    giro_type: str,
    creditor_name: str,
    amount: float,
    currency: str = "SEK",
    execution_date: date | None = None,
    invoice_reference: str = "",
    ocr_reference: str = "",
) -> dict[str, Any]:
    """Build a payment body for Bankgiro/Plusgiro (payment-product 'swedish-giro').

    Giro payments carry either an OCR reference or a free-text invoice reference, never both,
    so only the one that was supplied is included.
    """
    payload: dict[str, Any] = {
        "instructedAmount": {"amount": f"{amount:.2f}", "currency": currency},
        "debtorAccount": {"iban": debtor_iban, "currency": currency},
        "creditorGiro": {"giroNumber": giro_number, "giroType": giro_type},
        "creditorName": creditor_name,
        "requestedExecutionDate": (execution_date or date.today()).isoformat(),
    }
    if ocr_reference:
        payload["ocrRef"] = ocr_reference
    elif invoice_reference:
        payload["invoiceRef"] = invoice_reference
    return payload


def _payment_path(payment_product: str, payment_id: str) -> str:
    return f"/psd2/paymentinitiation/v1/payments/{payment_product}/{payment_id}"


def _preferred_sca_method(sca_methods: list[dict[str, Any]]) -> str:
    """Pick an SCA method, favouring BankID on a separate device."""
    if not sca_methods:
        raise OpenPaymentsError("The bank did not offer any SCA methods for this payment.")

    by_id = {method.get("authenticationMethodId", ""): method for method in sca_methods}
    for preferred in ("mbid_animated_qr_token", "mbid", "mbid_same_device"):
        if preferred in by_id:
            return preferred
    return sca_methods[0].get("authenticationMethodId", "")


def _raise_for_status(response: httpx.Response, action: str) -> None:
    if response.is_success:
        return
    raise OpenPaymentsError(
        f"Open Payments returned {response.status_code} while {action}: {response.text.strip()}"
    )
