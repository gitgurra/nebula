"""Settings loaded from environment variables. See .env.template for the full list."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


class ConfigError(RuntimeError):
    """Raised when required environment variables are missing."""


@dataclass(frozen=True)
class OpenPaymentsSettings:
    """Credentials and endpoints for the Open Payments (Open Banking Platform) API."""

    client_id: str
    client_secret: str
    auth_base_url: str
    api_base_url: str
    scope: str
    iso_country_codes: tuple[str, ...]
    pis_scope: str
    bicfi: str
    psu_id: str
    psu_corporate_id: str
    psu_ip_address: str
    psu_user_agent: str
    debtor_iban: str

    def missing_payment_settings(self) -> list[str]:
        """Report which payment initiation variables are unset, without failing.

        Only payment initiation needs these: the bank has to know which customer is
        authorising the payment and which account to debit. The read-only calls do not.
        """
        return [
            name
            for name, value in (
                ("OPEN_PAYMENTS_BICFI", self.bicfi),
                ("OPEN_PAYMENTS_PSU_ID", self.psu_id),
                ("OPEN_PAYMENTS_PSU_CORPORATE_ID", self.psu_corporate_id),
                ("OPEN_PAYMENTS_DEBTOR_IBAN", self.debtor_iban),
            )
            if not value
        ]

    def require_payment_settings(self) -> None:
        """Fail early when payment initiation is missing the PSU context the bank requires."""
        _raise_if_missing(self.missing_payment_settings())


@dataclass(frozen=True)
class ZwapgridSettings:
    """Credentials and endpoints for the Zwapgrid API.1 Consent and Accounting APIs."""

    api_key: str
    consents_base_url: str
    accounting_base_url: str


def load_env(env_file: str | os.PathLike[str] | None = None) -> None:
    """Load the local .env file. Real environment variables always win."""
    load_dotenv(env_file or PROJECT_ROOT / ".env")


def load_open_payments_settings() -> OpenPaymentsSettings:
    missing: list[str] = []
    client_id = _required("OPEN_PAYMENTS_CLIENT_ID", missing)
    client_secret = _required("OPEN_PAYMENTS_CLIENT_SECRET", missing)
    _raise_if_missing(missing)

    return OpenPaymentsSettings(
        client_id=client_id,
        client_secret=client_secret,
        auth_base_url=_optional(
            "OPEN_PAYMENTS_AUTH_BASE_URL", "https://auth.sandbox.openbankingplatform.com"
        ).rstrip("/"),
        api_base_url=_optional(
            "OPEN_PAYMENTS_API_BASE_URL", "https://api.sandbox.openbankingplatform.com"
        ).rstrip("/"),
        scope=_optional("OPEN_PAYMENTS_SCOPE", "aspspinformation corporate"),
        iso_country_codes=_csv("OPEN_PAYMENTS_ISO_COUNTRY_CODES", "SE"),
        pis_scope=_optional("OPEN_PAYMENTS_PIS_SCOPE", "paymentinitiation corporate"),
        bicfi=_optional("OPEN_PAYMENTS_BICFI", ""),
        psu_id=_optional("OPEN_PAYMENTS_PSU_ID", ""),
        psu_corporate_id=_optional("OPEN_PAYMENTS_PSU_CORPORATE_ID", ""),
        psu_ip_address=_optional("OPEN_PAYMENTS_PSU_IP_ADDRESS", "127.0.0.1"),
        psu_user_agent=_optional("OPEN_PAYMENTS_PSU_USER_AGENT", "nebula/0.1"),
        debtor_iban=_optional("OPEN_PAYMENTS_DEBTOR_IBAN", ""),
    )


def load_zwapgrid_settings() -> ZwapgridSettings:
    missing: list[str] = []
    api_key = _required("ZWAPGRID_API_KEY", missing)
    _raise_if_missing(missing)

    return ZwapgridSettings(
        api_key=api_key,
        consents_base_url=_optional(
            "ZWAPGRID_CONSENTS_BASE_URL", "https://apione.zwapgrid.com/consents"
        ).rstrip("/"),
        accounting_base_url=_optional(
            "ZWAPGRID_ACCOUNTING_BASE_URL", "https://apione.zwapgrid.com/accounting"
        ).rstrip("/"),
    )


def request_timeout_seconds() -> float:
    raw = _optional("REQUEST_TIMEOUT_SECONDS", str(DEFAULT_REQUEST_TIMEOUT_SECONDS))
    try:
        return float(raw)
    except ValueError as error:
        raise ConfigError(f"REQUEST_TIMEOUT_SECONDS must be a number, got '{raw}'.") from error


def _required(name: str, missing: list[str]) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        missing.append(name)
    return value


def _optional(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = _optional(name, default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _raise_if_missing(missing: list[str]) -> None:
    if missing:
        raise ConfigError(
            "Missing environment variables: "
            + ", ".join(missing)
            + ". Copy .env.template to .env and fill in your sandbox credentials."
        )
