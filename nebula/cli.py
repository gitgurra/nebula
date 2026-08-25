"""Command line entry point for the sandbox hello-world calls."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from .config import (
    ConfigError,
    load_env,
    load_open_payments_settings,
    load_zwapgrid_settings,
    request_timeout_seconds,
)
from .open_payments import OpenPaymentsClient, OpenPaymentsError
from .zwapgrid import ConsentStatus, ZwapgridClient, ZwapgridError, onboarding_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nebula",
        description="Hello-world calls against the Open Payments and Zwapgrid sandboxes.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="hello",
        choices=["hello", "open-payments", "zwapgrid", "create-consent"],
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
        default="testone",
        help=(
            "Accounting system to deep link to in the Onboarding Flow, e.g. testone (the Test.1 "
            "dummy system) or fortnox. Use 'any' to let the customer choose."
        ),
    )
    args = parser.parse_args(argv)

    load_env()

    if args.command == "create-consent":
        return 0 if run_create_consent(args.name, args.system) else 1

    runners: dict[str, list[Callable[[bool], bool]]] = {
        "hello": [run_open_payments, run_zwapgrid],
        "open-payments": [run_open_payments],
        "zwapgrid": [run_zwapgrid],
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
                    "\nNo accepted consent yet, so there is no ERP data to read. "
                    "Run 'python -m nebula create-consent' and complete the Onboarding Flow."
                )
                return True

            print(f"\nUsing accepted consent {accepted['id']} (source={accepted.get('source')}).")
            company = client.get_company_information(accepted["id"])
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
