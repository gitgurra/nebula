"""Client for the Zwapgrid API.1 Consent and Accounting APIs (https://docs.zwapgrid.com/)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from enum import IntEnum
from types import TracebackType
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import ZwapgridSettings


ACCOUNTING_SYSTEM = "Fortnox"


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

    def find_accepted_consent(self, source: str = ACCOUNTING_SYSTEM) -> dict[str, Any] | None:
        """Return the most recently accepted consent for `source`, or None if there is none.

        The source filter is what keeps a consent for some other accounting system from being
        picked up silently just because it happens to be the most recently accepted one.
        """
        page = self.list_consents(count=100, status="ACCEPTED")
        accepted = [
            consent
            for consent in page.get("data") or []
            if consent.get("status") == ConsentStatus.ACCEPTED
            and (consent.get("source") or "").casefold() == source.casefold()
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

    def get_supplier_invoices(
        self, consent_id: str, count: int = 100, current_page: int = 1
    ) -> dict[str, Any]:
        """Fetch one page of invoices the customer has received (accounts payable)."""
        return self._get_page(consent_id, "supplierinvoices", count, current_page)

    def get_sales_invoices(
        self, consent_id: str, count: int = 100, current_page: int = 1
    ) -> dict[str, Any]:
        """Fetch one page of invoices the customer has issued (accounts receivable)."""
        return self._get_page(consent_id, "salesinvoices", count, current_page)

    def get_all(
        self, consent_id: str, resource: str, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Page through every record of a resource, such as supplierinvoices or suppliers.

        Paging stops at the page count reported in the response: asking for a page past the
        end is a 400, not an empty list.
        """
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._get_page(consent_id, resource, page_size, page)
            rows = batch.get("data") or []
            records.extend(rows)
            total_pages = (batch.get("meta") or {}).get("totalPages") or 1
            if not rows or page >= total_pages:
                return records
            page += 1

    def get_invoice(self, consent_id: str, resource: str, invoice_id: str) -> dict[str, Any]:
        """Fetch one invoice with every field the system holds.

        The list endpoints return a 10-field summary; this returns 28, and `invoiceLines`
        and `paymentMeans` appear only here.
        """
        return self._get(
            f"{self._settings.accounting_base_url}"
            f"/api/v1/consents/{consent_id}/{resource}/{invoice_id}"
        )

    def get_all_invoices_in_full(
        self, consent_id: str, resource: str, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch every invoice of one kind in full.

        This costs one request per invoice on top of the paging, because the API offers no
        way to ask a list endpoint for the complete records.
        """
        return [
            self.get_invoice(consent_id, resource, invoice["id"])
            for invoice in self.get_all(consent_id, resource, page_size)
            if invoice.get("id")
        ]

    def get_suppliers(self, consent_id: str) -> list[dict[str, Any]]:
        """Fetch every supplier, including the accounts each one expects to be paid into.

        Fortnox keeps payee bank details on the supplier rather than on the invoice, under
        `paymentMeans[].financialAccount`. Only the list endpoint works: fetching a single
        supplier by ID returns 501.
        """
        return self.get_all(consent_id, "suppliers")

    def _get_page(
        self, consent_id: str, resource: str, count: int, current_page: int
    ) -> dict[str, Any]:
        return self._get(
            f"{self._settings.accounting_base_url}/api/v1/consents/{consent_id}/{resource}",
            params={"Count": count, "CurrentPage": current_page},
        )

    def create_supplier_invoice(self, consent_id: str, invoice: dict[str, Any]) -> str | None:
        """Create a supplier invoice for an accepted consent, returning the new invoice ID.

        A successful create responds 201 with an empty body, so the ID is only available
        from the Location header. See
        https://docs.zwapgrid.com/api-guide/accounting-api-guide/supplier-invoices
        for the expected request body shape.
        """
        response = self._send(
            "POST",
            f"{self._settings.accounting_base_url}/api/v1/consents/{consent_id}/supplierinvoices",
            json=invoice,
        )
        location = response.headers.get("Location", "")
        return location.rstrip("/").rsplit("/", 1)[-1] or None

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


def supplier_invoice_payload(
    reference: str,
    supplier_id: str = "supplier-123",
    supplier_name: str = "Nebula Test Supplier AB",
    amount: float = 1000.0,
    currency: str = "SEK",
    issue_date: date | None = None,
    due_date: date | None = None,
    payment_account: str = "5555-6666",
    payment_scheme_id: str = "SE:BG",
    payment_channel_code: str = "BANK_TRANSFER",
    line_description: str = "Consulting services",
    account_id: str = "6550",
) -> dict[str, Any]:
    """Build a supplier invoice body for POST /supplierinvoices.

    The schema marks every field nullable, but the connected accounting system applies its
    own rules on top: Fortnox rejects an invoice with no lines, no supplier account ID or
    no issue date. Each line also needs either an accounting account or a seller item ID,
    and both must already exist in the connected system: an item ID has to match an article
    in the register, so a cost account is used here instead.

    `paymentIds[].id` carries the account the supplier expects to be paid into, and is the
    value a fraud check compares against the bank's `creditorAccount`. `schemeId` names the
    numbering scheme that value belongs to, such as SE:BG for a bankgiro. The write model has
    no `financialAccount`, so these identifiers are the only place to put the payee account.
    """
    issued = issue_date or date.today()
    due = due_date or issued + timedelta(days=30)
    total = {"amount": amount, "currencyId": currency}
    return {
        "reference": reference,
        "issueDate": issued.isoformat(),
        "dueDate": due.isoformat(),
        "documentCurrencyCode": {"currencyId": currency},
        "accountingSupplierParty": {
            "customerAssignedAccountId": {"id": supplier_id},
            "party": {"partyName": {"name": supplier_name}},
        },
        "paymentMeans": [
            {
                "paymentChannelCode": payment_channel_code,
                "paymentDueDate": due.isoformat(),
                "paymentIds": [{"id": payment_account, "schemeId": payment_scheme_id}],
            }
        ],
        "invoiceLines": [
            {
                "id": "1",
                "invoicedQuantity": {"quantity": 1},
                "lineExtensionAmount": total,
                "account": {"id": account_id, "accountingAccountId": account_id},
                "item": {"name": line_description},
                "price": {"priceAmount": total},
            }
        ],
        "totalBalanceAmount": total,
        "legalMonetaryTotal": {"payableAmount": total, "taxInclusiveAmount": total},
    }


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
