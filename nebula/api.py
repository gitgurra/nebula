"""FastAPI app exposing mocked invoice endpoints.

This is not yet wired up to Open Payments or Zwapgrid. The list is seeded from
data/invoices.json and the per-invoice details from data/invoices/*.json, so the
response shapes match those fixtures. Every endpoint only mutates the in-memory
copy, so nothing is written back to disk and the data resets whenever the
process restarts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "invoices.json"
DETAIL_DIR = (Path(__file__).resolve().parent.parent / "data" / "invoices").resolve()

# The signed-in user in this mock. A real approver comes from the session.
APPROVER = "M. Lindqvist"

app = FastAPI(
    title="Nebula Invoices API",
    description="Mocked invoice endpoints for get_invoices and pay_invoice.",
    version="0.1.0",
)

# This is a local mock with no auth and no sensitive data, so it's fine to allow
# any origin — e.g. opening invoice-hold-standalone.html directly as a file.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class Supplier(BaseModel):
    name: str
    orgNumber: str


class Amount(BaseModel):
    value: str
    currency: str


class Invoice(BaseModel):
    id: str
    number: str
    supplier: Supplier
    dueDate: str
    amount: Amount
    status: str
    statusLabel: str
    href: str
    hasDetail: bool = False


class InvoiceListMeta(BaseModel):
    totalResources: int
    counts: dict[str, int]
    tenant: dict[str, str]
    period: str
    generatedAt: str


class InvoiceList(BaseModel):
    data: list[Invoice]
    meta: InvoiceListMeta


class PaymentResult(BaseModel):
    invoice_id: str
    status: str
    paid_amount: Amount
    paid_at: datetime


class VerificationCall(BaseModel):
    """What the person who called the supplier recorded."""

    contact: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=1, max_length=600)


class VerificationCallResult(BaseModel):
    invoice_id: str
    status: str
    status_label: str
    logged_at: datetime


def _apply_action_availability(detail: dict[str, Any]) -> None:
    """Derive which actions an invoice offers from its status.

    Held invoices offer the verification call and the override; anything not
    held and not already paid can be paid. Keeping this in one place is what
    stops a released invoice from still advertising a hold action.
    """
    status = detail.get("status")
    actions = detail.setdefault("actions", {})
    held = status == "hold"

    actions.setdefault("pay", {})["available"] = not held and status != "paid"
    for key in ("logVerificationCall", "keepOnHold", "release"):
        if key in actions:
            actions[key]["available"] = held


def _load_details() -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for path in sorted(DETAIL_DIR.glob("*.json")):
        detail = json.loads(path.read_text())
        _apply_action_availability(detail)
        details[detail["id"]] = detail
    return details


def _load_invoices(
    details: dict[str, dict[str, Any]],
) -> tuple[dict[str, Invoice], InvoiceListMeta]:
    raw = json.loads(DATA_FILE.read_text())
    invoices = {
        item["id"]: Invoice(**item, hasDetail=item["id"] in details) for item in raw["data"]
    }
    return invoices, InvoiceListMeta(**raw["meta"])


# In-memory mock store, seeded from the fixtures. Reset whenever the process
# restarts.
_details = _load_details()
_invoices, _meta = _load_invoices(_details)
_lock = Lock()


def _require_invoice(invoice_id: str) -> Invoice:
    invoice = _invoices.get(invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail=f"Invoice '{invoice_id}' not found.")
    return invoice


def _invoice_account(detail: dict[str, Any]) -> str | None:
    """The account this invoice asks to be paid to, per the bank details check."""
    findings = detail.get("bankDetailsCheck", {}).get("findings", [])
    for finding in findings:
        evidence = finding.get("evidence") or {}
        if evidence.get("invoiceAccount"):
            return evidence["invoiceAccount"]
    return None


def _decrement_count(key: str) -> None:
    if key in _meta.counts:
        _meta.counts[key] = max(0, _meta.counts[key] - 1)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/invoices", response_model=InvoiceList)
def get_invoices() -> InvoiceList:
    """Return every mocked invoice, in the same shape as data/invoices.json."""
    with _lock:
        return InvoiceList(data=list(_invoices.values()), meta=_meta)


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str) -> dict[str, Any]:
    """Return the full mocked detail for one invoice, e.g. bank details check findings."""
    with _lock:
        detail = _details.get(invoice_id)
        if detail is None:
            raise HTTPException(
                status_code=404, detail=f"Invoice '{invoice_id}' has no detail fixture."
            )
        return detail


@app.post("/invoices/{invoice_id}/pay", response_model=PaymentResult)
def pay_invoice(invoice_id: str) -> PaymentResult:
    """Mark a mocked invoice as paid and return the payment confirmation."""
    with _lock:
        invoice = _require_invoice(invoice_id)
        if invoice.status == "paid":
            raise HTTPException(
                status_code=409, detail=f"Invoice '{invoice_id}' is already paid."
            )
        if invoice.status == "hold":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Invoice '{invoice_id}' is on hold. Log a verification call to "
                    "release it before paying."
                ),
            )

        paid_at = datetime.now(timezone.utc)
        _invoices[invoice_id] = invoice.model_copy(
            update={"status": "paid", "statusLabel": "Paid"}
        )

        detail = _details.get(invoice_id)
        if detail is not None:
            detail["status"] = "paid"
            payment = detail.get("payment") or {}
            payment.update(
                {
                    "statusLabel": "Paid",
                    "paymentDate": paid_at.date().isoformat(),
                    "approver": APPROVER,
                }
            )
            payment.setdefault("account", _invoice_account(detail) or "—")
            detail["payment"] = payment
            _apply_action_availability(detail)

        return PaymentResult(
            invoice_id=invoice_id,
            status="paid",
            paid_amount=invoice.amount,
            paid_at=paid_at,
        )


@app.post("/invoices/{invoice_id}/verification-call", response_model=VerificationCallResult)
def log_verification_call(invoice_id: str, call: VerificationCall) -> VerificationCallResult:
    """Record a call to the supplier's register number and release the hold.

    The call is the evidence, so it lands in the findings list rather than only
    flipping the status.
    """
    with _lock:
        invoice = _require_invoice(invoice_id)
        detail = _details.get(invoice_id)
        if detail is None:
            raise HTTPException(
                status_code=404, detail=f"Invoice '{invoice_id}' has no detail fixture."
            )
        if invoice.status != "hold":
            raise HTTPException(
                status_code=409, detail=f"Invoice '{invoice_id}' is not on hold."
            )

        logged_at = datetime.now(timezone.utc)
        call_action = detail.get("actions", {}).get("logVerificationCall") or {}
        call_number = call_action.get("callNumber", "the register number")

        check = detail.setdefault("bankDetailsCheck", {})
        check["verdict"] = "verified"
        check["headline"] = "Account change verified with the supplier"
        check["detail"] = (
            f"{call.contact} confirmed the new account on {call_number}. "
            "The hold is released and the payment can be signed."
        )
        check.setdefault("findings", []).append(
            {
                "code": "verification_call",
                "title": "Verification call logged",
                "detail": f"{call.contact} on {call_number}: {call.note}",
                "outcome": "match",
                "outcomeLabel": "Verified",
                "sources": ["Verification call"],
                "mocked": False,
                "evidence": None,
            }
        )

        detail["status"] = "verified"
        detail["payment"] = {
            "statusLabel": "Released for payment",
            "paymentDate": detail.get("dueDate", ""),
            "account": _invoice_account(detail) or "—",
            "approver": "Awaiting signature",
        }
        _apply_action_availability(detail)

        _invoices[invoice_id] = invoice.model_copy(
            update={"status": "verified", "statusLabel": "Verified"}
        )
        _decrement_count("hold")
        _decrement_count("awaitingVerification")

        return VerificationCallResult(
            invoice_id=invoice_id,
            status="verified",
            status_label="Verified",
            logged_at=logged_at,
        )
