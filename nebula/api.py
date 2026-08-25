"""FastAPI app exposing mocked invoice endpoints.

This is not yet wired up to Open Payments or Zwapgrid — both endpoints operate
on an in-memory list of invoices so the API shape can be worked out first.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from threading import Lock

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(
    title="Nebula Invoices API",
    description="Mocked invoice endpoints for get_invoices and pay_invoice.",
    version="0.1.0",
)


class InvoiceStatus(str, Enum):
    UNPAID = "unpaid"
    PAID = "paid"


class Invoice(BaseModel):
    id: str
    invoice_number: str
    supplier_id: str
    currency: str
    total_amount: float
    due_date: date
    status: InvoiceStatus


class PaymentResult(BaseModel):
    invoice_id: str
    status: InvoiceStatus
    paid_amount: float
    paid_at: datetime


def _seed_invoices() -> dict[str, Invoice]:
    seed = [
        Invoice(
            id="inv-001",
            invoice_number="INV-001",
            supplier_id="supplier-123",
            currency="SEK",
            total_amount=1000.0,
            due_date=date(2026, 9, 30),
            status=InvoiceStatus.UNPAID,
        ),
        Invoice(
            id="inv-002",
            invoice_number="INV-002",
            supplier_id="supplier-456",
            currency="EUR",
            total_amount=2500.5,
            due_date=date(2026, 10, 15),
            status=InvoiceStatus.UNPAID,
        ),
        Invoice(
            id="inv-003",
            invoice_number="INV-003",
            supplier_id="supplier-123",
            currency="SEK",
            total_amount=430.0,
            due_date=date(2026, 8, 1),
            status=InvoiceStatus.PAID,
        ),
    ]
    return {invoice.id: invoice for invoice in seed}


# In-memory mock store. Reset whenever the process restarts.
_invoices: dict[str, Invoice] = _seed_invoices()
_lock = Lock()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/invoices", response_model=list[Invoice])
def get_invoices() -> list[Invoice]:
    """Return every mocked invoice."""
    with _lock:
        return list(_invoices.values())


@app.post("/invoices/{invoice_id}/pay", response_model=PaymentResult)
def pay_invoice(invoice_id: str) -> PaymentResult:
    """Mark a mocked invoice as paid and return the payment confirmation."""
    with _lock:
        invoice = _invoices.get(invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail=f"Invoice '{invoice_id}' not found.")
        if invoice.status == InvoiceStatus.PAID:
            raise HTTPException(
                status_code=409, detail=f"Invoice '{invoice_id}' is already paid."
            )

        paid_invoice = invoice.model_copy(update={"status": InvoiceStatus.PAID})
        _invoices[invoice_id] = paid_invoice

        return PaymentResult(
            invoice_id=invoice_id,
            status=InvoiceStatus.PAID,
            paid_amount=paid_invoice.total_amount,
            paid_at=datetime.now(timezone.utc),
        )
