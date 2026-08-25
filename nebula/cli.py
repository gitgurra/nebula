"""Command line entry point for the sandbox hello-world calls."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import (
    ConfigError,
    load_env,
    load_open_payments_settings,
    load_zwapgrid_settings,
    request_timeout_seconds,
)
from .open_payments import (
    OpenPaymentsClient,
    OpenPaymentsError,
    domestic_payment_payload,
    swedish_giro_payment_payload,
)
from .zwapgrid import (
    ConsentStatus,
    ZwapgridClient,
    ZwapgridError,
    onboarding_url,
    supplier_invoice_payload,
    supplier_invoice_payment_payload,
    supplier_payee_account,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nebula",
        description="Hello-world calls against the Open Payments and Zwapgrid sandboxes.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="hello",
        choices=[
            "hello",
            "open-payments",
            "zwapgrid",
            "create-consent",
            "zwapgrid_create_invoices",
            "zwapgrid_invoices",
            "zwapgrid_suppliers",
            "pay-invoice",
        ],
        help="Which sandbox to call. Defaults to 'hello', which calls both.",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON responses.")
    parser.add_argument(
        "--name",
        default="Nebula sandbox consent",
        help="Display name for the consent created by 'create-consent'.",
    )
    parser.add_argument(
        "--system",
        default="fortnox",
        help=(
            "Accounting system to deep link to in the Onboarding Flow. Use 'any' to let the "
            "customer choose."
        ),
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of invoices to create when running 'zwapgrid_create_invoices'.",
    )
    parser.add_argument(
        "--out",
        nargs="?",
        const="invoices.json",
        help=(
            "Write the invoices fetched by 'zwapgrid_invoices' to a JSON file. Defaults to "
            "invoices.json when the flag is given without a path."
        ),
    )
    parser.add_argument(
        "--supplier-id",
        default="supplier-123",
        help=(
            "Supplier number to invoice from. The supplier must already exist in the connected "
            "accounting system; Fortnox rejects unknown suppliers."
        ),
    )
    parser.add_argument(
        "--account",
        default="6550",
        help=(
            "Cost account to book the invoice line to. Must exist in the chart of accounts "
            "(6550 is Konsultarvoden in the Swedish BAS chart)."
        ),
    )
    parser.add_argument(
        "--invoice",
        help="Reference of the supplier invoice to pay with 'pay-invoice', e.g. INV-007.",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help=(
            "Actually initiate the payment in 'pay-invoice'. Without it the command is a dry "
            "run that only prints the request it would send."
        ),
    )
    parser.add_argument(
        "--bank-account",
        default="1930",
        help=(
            "Asset account the payment is booked against in the ERP (1930 is Företagskonto "
            "in the Swedish BAS chart)."
        ),
    )
    args = parser.parse_args(argv)

    load_env()

    if args.command == "create-consent":
        return 0 if run_create_consent(args.name, args.system) else 1

    if args.command == "pay-invoice":
        if not args.invoice:
            parser.error("pay-invoice needs --invoice with the reference of the invoice to pay.")
        return (
            0
            if run_pay_invoice(args.json, args.invoice, args.send, args.bank_account)
            else 1
        )

    runners: dict[str, list[Callable[[bool], bool]]] = {
        "hello": [run_open_payments, run_zwapgrid],
        "open-payments": [run_open_payments],
        "zwapgrid": [run_zwapgrid],
        "zwapgrid_create_invoices": [
            lambda as_json: zwapgrid_create_invoices(
                as_json, args.count, args.supplier_id, args.account
            )
        ],
        "zwapgrid_invoices": [lambda as_json: zwapgrid_invoices(as_json, args.out)],
        "zwapgrid_suppliers": [zwapgrid_suppliers],
    }

    results = [runner(args.json) for runner in runners[args.command]]
    return 0 if all(results) else 1


def run_open_payments(as_json: bool) -> bool:
    """Fetch an access token and list the banks available in the sandbox."""
    _header("Open Payments")
    try:
        settings = load_open_payments_settings()
        with OpenPaymentsClient(settings, timeout=request_timeout_seconds()) as client:
            print(f"Auth host: {settings.auth_base_url}")
            print(f"API host:  {settings.api_base_url}")
            client.access_token()
            print(f"Access token acquired for scope '{settings.scope}'.")

            banks = client.list_banks()
            countries = ", ".join(settings.iso_country_codes) or "all countries"
            print(f"Found {len(banks)} bank(s) for {countries}:")
            if as_json:
                _dump(banks)
            else:
                for bank in banks:
                    print(f"  - {bank.get('bicFi')}  {bank.get('name')}")
    except (ConfigError, OpenPaymentsError) as error:
        return _fail(error)
    return True


def run_zwapgrid(as_json: bool) -> bool:
    """List consents and read ERP company information from the accepted one, if there is one."""
    _header("Zwapgrid")
    try:
        settings = load_zwapgrid_settings()
        with ZwapgridClient(settings, timeout=request_timeout_seconds()) as client:
            print(f"Consents API:   {settings.consents_base_url}")
            print(f"Accounting API: {settings.accounting_base_url}")

            page = client.list_consents()
            consents = page.get("data") or []
            total = (page.get("meta") or {}).get("totalResources", len(consents))
            print(f"Found {total} consent(s), showing {len(consents)}:")
            if as_json:
                _dump(page)
            else:
                for consent in consents:
                    print(
                        f"  - {consent.get('id')}  {consent.get('name')} "
                        f"(status={_status_name(consent.get('status'))}, "
                        f"source={consent.get('source')})"
                    )

            accepted = client.find_accepted_consent()
            if accepted is None:
                print(
                    "\nNo accepted Fortnox consent yet, so there is no ERP data to read. "
                    "Run 'python -m nebula create-consent' and complete the Onboarding Flow."
                )
                return True

            print(f"\nUsing accepted consent {accepted['id']} (source={accepted.get('source')}).")

            company = client.get_company_information(consent_id=accepted["id"])
            print("Company information:")
            if as_json:
                _dump(company)
            else:
                party_name = (company.get("partyName") or {}).get("name")
                legal_entity = company.get("partyLegalEntity") or {}
                company_id = (legal_entity.get("companyId") or {}).get("id")
                print(f"  name:       {party_name}")
                print(f"  company id: {company_id}")
    except (ConfigError, ZwapgridError) as error:
        return _fail(error)
    return True

def zwapgrid_invoices(as_json: bool, out: str | None = None) -> bool:
    """Print every sales and supplier invoice on the accepted consent, in full."""
    _header("Zwapgrid: all invoices")
    try:
        settings = load_zwapgrid_settings()
        with ZwapgridClient(settings, timeout=request_timeout_seconds()) as client:
            accepted = client.find_accepted_consent()
            if accepted is None:
                print(
                    "No accepted Fortnox consent yet, so there are no invoices to read. "
                    "Run 'python -m nebula create-consent' and complete the Onboarding Flow."
                )
                return True

            print(f"Using accepted consent {accepted['id']} (source={accepted.get('source')}).")

            collected: dict[str, list[dict[str, Any]]] = {}
            for title, resource in (
                ("Sales invoices", "salesinvoices"),
                ("Supplier invoices", "supplierinvoices"),
            ):
                try:
                    invoices = client.get_all_invoices_in_full(accepted["id"], resource)
                except ZwapgridError as error:
                    print(f"\n{title}: unavailable ({error})")
                    continue

                collected[resource] = invoices
                print(f"\n{title}: {len(invoices)}")
                _dump(invoices)

            if out:
                _save_invoices(out, accepted, collected)
    except (ConfigError, ZwapgridError) as error:
        return _fail(error)
    return True


def _save_invoices(
    path: str, consent: dict[str, Any], invoices: dict[str, list[dict[str, Any]]]
) -> None:
    """Write the fetched invoices to the repo root, recording where and when they came from.

    A relative path is resolved against the repo root rather than the working directory, so
    the file lands in the same place wherever the command is run from. The provenance matters
    for a fraud check: a stored invoice is only meaningful next to the consent and system it
    was read from, and the time it was read.
    """
    snapshot = {
        "consentId": consent.get("id"),
        "source": consent.get("source"),
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "salesInvoices": invoices.get("salesinvoices", []),
        "supplierInvoices": invoices.get("supplierinvoices", []),
    }
    destination = Path(path)
    if not destination.is_absolute():
        destination = Path(__file__).resolve().parent.parent / destination
    destination.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    total = len(snapshot["salesInvoices"]) + len(snapshot["supplierInvoices"])
    print(f"\nWrote {total} invoice(s) to {destination.resolve()}")


def zwapgrid_suppliers(as_json: bool) -> bool:
    """Print every supplier in full, including the accounts each expects to be paid into."""
    _header("Zwapgrid: suppliers")
    try:
        settings = load_zwapgrid_settings()
        with ZwapgridClient(settings, timeout=request_timeout_seconds()) as client:
            accepted = client.find_accepted_consent()
            if accepted is None:
                print(
                    "No accepted Fortnox consent yet, so there are no suppliers to read. "
                    "Run 'python -m nebula create-consent' and complete the Onboarding Flow."
                )
                return True

            print(f"Using accepted consent {accepted['id']} (source={accepted.get('source')}).")
            suppliers = client.get_suppliers(accepted["id"])
            print(f"\nSuppliers: {len(suppliers)}")
            _dump(suppliers)
    except (ConfigError, ZwapgridError) as error:
        return _fail(error)
    return True


def _next_invoice_number(client: ZwapgridClient, consent_id: str, prefix: str = "INV-") -> int:
    """Continue the INV-NNN series, since invoices cannot be deleted once created."""
    existing = client.get_all(consent_id, "supplierinvoices")
    used = [
        int(match.group(1))
        for match in (
            re.fullmatch(rf"{re.escape(prefix)}(\d+)", str(invoice.get("reference") or ""))
            for invoice in existing
        )
        if match
    ]
    return max(used, default=0) + 1


def zwapgrid_create_invoices(
    as_json: bool,
    count: int = 1,
    supplier_id: str = "supplier-123",
    account_id: str = "6550",
) -> bool:
    """List consents and create `count` supplier invoices on the accepted one, if there is one."""
    _header("Zwapgrid")
    try:
        settings = load_zwapgrid_settings()
        with ZwapgridClient(settings, timeout=request_timeout_seconds()) as client:
            print(f"Consents API:   {settings.consents_base_url}")
            print(f"Accounting API: {settings.accounting_base_url}")

            page = client.list_consents()
            consents = page.get("data") or []
            total = (page.get("meta") or {}).get("totalResources", len(consents))
            print(f"Found {total} consent(s), showing {len(consents)}:")
            if as_json:
                _dump(page)
            else:
                for consent in consents:
                    print(
                        f"  - {consent.get('id')}  {consent.get('name')} "
                        f"(status={_status_name(consent.get('status'))}, "
                        f"source={consent.get('source')})"
                    )

            accepted = client.find_accepted_consent()
            if accepted is None:
                print(
                    "\nNo accepted Fortnox consent yet, so there is no ERP data to read. "
                    "Run 'python -m nebula create-consent' and complete the Onboarding Flow."
                )
                return True

            print(f"\nUsing accepted consent {accepted['id']} (source={accepted.get('source')}).")

            start = _next_invoice_number(client, accepted["id"])
            print(f"Creating {count} supplier invoice(s) from INV-{start:03d}")

            for i in range(count):
                invoice_number = f"INV-{start + i:03d}"
                supplier_invoice = client.create_supplier_invoice(
                    consent_id=accepted["id"],
                    invoice=supplier_invoice_payload(
                        reference=invoice_number,
                        supplier_id=supplier_id,
                        account_id=account_id,
                    ),
                )
                print(f"New invoice ({invoice_number}): {supplier_invoice}")

            supplier_invoices = client.get_supplier_invoices(consent_id=accepted["id"])
            print("Supplier invoices:")

            if as_json:
                _dump(supplier_invoices)

    except (ConfigError, ZwapgridError) as error:
        return _fail(error)
    return True


def run_pay_invoice(
    as_json: bool,
    reference: str,
    send: bool = False,
    bank_account: str = "1930",
) -> bool:
    """Pay one supplier invoice: initiate it in the bank, then book it back in the ERP.

    Nothing is sent unless `send` is set. The dry run prints the exact requests, which is the
    only safe default while the accepted consent points at a real Fortnox company.
    """
    _header(f"Pay supplier invoice {reference}")
    try:
        op_settings = load_open_payments_settings()
        zg_settings = load_zwapgrid_settings()

        with ZwapgridClient(zg_settings, timeout=request_timeout_seconds()) as zwapgrid:
            accepted = zwapgrid.find_accepted_consent()
            if accepted is None:
                print(
                    "No accepted Fortnox consent yet. Run 'python -m nebula create-consent' "
                    "and complete the Onboarding Flow."
                )
                return True
            consent_id = accepted["id"]
            print(f"Consent: {consent_id} (source={accepted.get('source')})")

            invoice = _find_supplier_invoice(zwapgrid, consent_id, reference)
            if invoice is None:
                return _fail(
                    RuntimeError(
                        f"No supplier invoice with reference '{reference}'. Create one with "
                        "'python -m nebula zwapgrid_create_invoices'."
                    )
                )

            amount = _invoice_amount(invoice)
            if amount is None:
                return _fail(RuntimeError(f"Invoice {reference} has no payable amount."))

            supplier_id, supplier_name = _invoice_supplier(invoice)
            payee = _resolve_payee(zwapgrid, consent_id, supplier_id, invoice)
            if payee is None:
                return _fail(
                    RuntimeError(
                        f"Supplier '{supplier_id}' has no payment account on record, so there "
                        "is nowhere to send the money. Add one in Fortnox."
                    )
                )

            currency = _invoice_currency(invoice)
            product, payment = _build_payment(
                op_settings.debtor_iban, payee, supplier_name, amount, currency, reference
            )

            print(f"\nInvoice:  {reference}  {amount:,.2f} {currency}")
            print(f"Supplier: {supplier_id}  {supplier_name}")
            print(f"Payee:    {payee['account']} ({payee['channelCode'] or payee['schemeId']})")
            print(f"Product:  {product}")
            print(f"\nPOST /psd2/paymentinitiation/v1/payments/{product}")
            _dump(payment)

            if not send:
                print(
                    "\nDry run. Nothing was sent. Re-run with --send to initiate the payment, "
                    "authorise it with BankID and book it in Fortnox."
                )
                missing = op_settings.missing_payment_settings()
                if missing:
                    print(
                        "\n--send needs these in .env first, because the bank has to know who "
                        "is authorising the payment and which account to debit:\n  "
                        + "\n  ".join(missing)
                    )
                return True

            with OpenPaymentsClient(op_settings, timeout=request_timeout_seconds()) as bank:
                created = bank.create_payment(product, payment)
                payment_id = created.get("paymentId")
                print(f"\nPayment created: {payment_id} ({created.get('transactionStatus')})")
                for message in created.get("tppMessages") or []:
                    print(f"  {message.get('category')}: {message.get('text')}")
                if not payment_id:
                    return _fail(RuntimeError("Payment response did not contain a paymentId."))

                print("Authorising. Approve the payment in your banking app.")
                sca_status = bank.authorise_payment(product, payment_id)
                print(f"SCA status: {sca_status}")
                if sca_status != "finalised":
                    return _fail(RuntimeError(f"Authorisation ended as '{sca_status}'."))

                status = bank.get_payment_status(product, payment_id)
                transaction_status = status.get("transactionStatus")
                print(f"Payment status: {transaction_status}")
                if transaction_status == "RJCT":
                    return _fail(RuntimeError("The bank rejected the payment."))

            booked = zwapgrid.create_supplier_invoice_payment(
                consent_id,
                invoice["id"],
                supplier_invoice_payment_payload(
                    reference=payment_id,
                    amount=amount,
                    currency=currency,
                    bank_account_id=bank_account,
                ),
            )
            print(f"\nBooked in Fortnox as payment {booked}, referencing {payment_id}.")

            if as_json:
                _dump(zwapgrid.get_supplier_invoice_payments(consent_id, invoice["id"]))
    except (ConfigError, OpenPaymentsError, ZwapgridError) as error:
        return _fail(error)
    return True


def _find_supplier_invoice(
    client: ZwapgridClient, consent_id: str, reference: str
) -> dict[str, Any] | None:
    """Look up one supplier invoice by its reference and return it in full."""
    for summary in client.get_all(consent_id, "supplierinvoices"):
        if str(summary.get("reference") or "") == reference and summary.get("id"):
            return client.get_invoice(consent_id, "supplierinvoices", summary["id"])
    return None


def _invoice_amount(invoice: dict[str, Any]) -> float | None:
    """Find what is left to pay on an invoice.

    Fortnox populates these inconsistently: `payableAmount` and `totalBalanceAmount` both come
    back as 0.00 on an invoice it has not settled, with the real total only in
    `taxInclusiveAmount`. A zero is therefore treated as absent rather than as a free invoice.
    """
    totals = invoice.get("legalMonetaryTotal") or {}
    for holder in (
        totals.get("payableAmount") or {},
        invoice.get("totalBalanceAmount") or {},
        totals.get("taxInclusiveAmount") or {},
    ):
        amount = holder.get("amount")
        if amount:
            return float(amount)
    return None


def _invoice_currency(invoice: dict[str, Any]) -> str:
    currency = (invoice.get("documentCurrencyCode") or {}).get("currencyId")
    return currency or "SEK"


def _invoice_supplier(invoice: dict[str, Any]) -> tuple[str, str]:
    party = invoice.get("accountingSupplierParty") or {}
    supplier_id = (party.get("customerAssignedAccountId") or {}).get("id") or ""
    name = ((party.get("party") or {}).get("partyName") or {}).get("name") or ""
    return str(supplier_id), str(name)


def _resolve_payee(
    client: ZwapgridClient, consent_id: str, supplier_id: str, invoice: dict[str, Any]
) -> dict[str, str] | None:
    """Find the account to pay, preferring the supplier record over the invoice.

    The supplier record is the more trustworthy of the two: an invoice arrives from outside
    and its payment details can be tampered with, while the supplier record only changes if
    someone edits it in the ERP.
    """
    for supplier in client.get_suppliers(consent_id):
        identifier = (supplier.get("customerAssignedAccountId") or {}).get("id")
        if str(identifier or "") == supplier_id:
            payee = supplier_payee_account(supplier)
            if payee:
                return payee
            break
    return supplier_payee_account(invoice)


def _build_payment(
    debtor_iban: str,
    payee: dict[str, str],
    creditor_name: str,
    amount: float,
    currency: str,
    reference: str,
) -> tuple[str, dict[str, Any]]:
    """Pick the payment product the payee account calls for and build its body.

    Fortnox labels the account with `paymentChannelCode` (BG, PG, IBAN) and leaves schemeId
    empty; an invoice written by this tool carries a schemeId instead. Both are checked so a
    bankgiro is never mistaken for an IBAN and paid as a plain transfer.
    """
    giro_types = {"BG": "BANKGIRO", "PG": "PLUSGIRO", "SE:BG": "BANKGIRO", "SE:PG": "PLUSGIRO"}
    giro_type = giro_types.get(payee["channelCode"].upper()) or giro_types.get(
        payee["schemeId"].upper()
    )
    if giro_type:
        return "swedish-giro", swedish_giro_payment_payload(
            debtor_iban=debtor_iban,
            giro_number=payee["account"],
            giro_type=giro_type,
            creditor_name=creditor_name,
            amount=amount,
            currency=currency,
            invoice_reference=reference,
        )
    return "domestic", domestic_payment_payload(
        debtor_iban=debtor_iban,
        creditor_iban=payee["account"],
        creditor_name=creditor_name,
        amount=amount,
        currency=currency,
        remittance_information=reference,
    )


def run_create_consent(name: str, system: str) -> bool:
    """Create a consent plus a one-time code and print the Onboarding Flow URL to open."""
    _header("Zwapgrid: create consent")
    try:
        settings = load_zwapgrid_settings()
        with ZwapgridClient(settings, timeout=request_timeout_seconds()) as client:
            consent_id = client.create_consent(name)
            print(f"Consent created: {consent_id}")

            one_time_code = client.create_one_time_code(consent_id)
            url = onboarding_url(
                consent_id, one_time_code, system=None if system == "any" else system
            )
            print("\nOpen this URL in a browser to connect an accounting system:")
            print(f"  {url}")
            print("\nThe one-time code is single-use and expires after one hour.")
            print("Once accepted, run 'python -m nebula zwapgrid' - the accepted consent is")
            print("picked up automatically, so there is nothing to copy into .env.")
    except (ConfigError, ZwapgridError) as error:
        return _fail(error)
    return True


def _header(title: str) -> None:
    print(f"\n=== {title} ===")


def _status_name(status: Any) -> str:
    try:
        return ConsentStatus(status).name
    except ValueError:
        return str(status)


def _dump(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _fail(error: Exception) -> bool:
    sys.stdout.flush()
    print(f"FAILED: {error}", file=sys.stderr)
    return False
