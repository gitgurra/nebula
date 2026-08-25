"""FastAPI app exposing mocked invoice endpoints.

This is not yet wired up to Open Payments or Zwapgrid. Invoice data is seeded
from data/invoices.json so the API response shape matches that fixture; both
endpoints only ever mutate the in-memory copy, so nothing is written back to
disk and the data resets whenever the process restarts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "invoices.json"
DETAIL_DIR = (Path(__file__).resolve().parent.parent / "data" / "invoices").resolve()

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


def _load_invoices() -> tuple[dict[str, Invoice], InvoiceListMeta]:
    raw = json.loads(DATA_FILE.read_text())
    invoices = {item["id"]: Invoice(**item) for item in raw["data"]}
    return invoices, InvoiceListMeta(**raw["meta"])


# In-memory mock store, seeded from data/invoices.json. Reset whenever the
# process restarts.
_invoices, _meta = _load_invoices()
_lock = Lock()


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
    detail_file = (DETAIL_DIR / f"{invoice_id}.json").resolve()
    if DETAIL_DIR not in detail_file.parents or not detail_file.is_file():
        raise HTTPException(
            status_code=404, detail=f"Invoice '{invoice_id}' has no detail fixture."
        )
    return json.loads(detail_file.read_text())


@app.post("/invoices/{invoice_id}/pay", response_model=PaymentResult)
def pay_invoice(invoice_id: str) -> PaymentResult:
    """Mark a mocked invoice as paid and return the payment confirmation."""
    with _lock:
        invoice = _invoices.get(invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail=f"Invoice '{invoice_id}' not found.")
        if invoice.status == "paid":
            raise HTTPException(
                status_code=409, detail=f"Invoice '{invoice_id}' is already paid."
            )

        paid_invoice = invoice.model_copy(update={"status": "paid", "statusLabel": "Paid"})
        _invoices[invoice_id] = paid_invoice

        return PaymentResult(
            invoice_id=invoice_id,
            status="paid",
            paid_amount=paid_invoice.amount,
            paid_at=datetime.now(timezone.utc),
        )
