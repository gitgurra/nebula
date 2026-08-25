# Nebula

A small local Python app that talks to two sandbox APIs:

- **[Open Payments](https://docs.openpayments.io/)** — PSD2 open banking (bank accounts, balances, transactions).
- **[Zwapgrid API.1](https://docs.zwapgrid.com/)** — unified access to accounting/ERP systems.

Right now it only does the plumbing plus a hello-world call against each API. The eventual goal is to
compare bank account data from Open Payments with the ERP data for the same account in Zwapgrid.

## Requirements

- Python 3.9 or newer
- Sandbox credentials for both APIs (see below)

## Getting credentials

### Open Payments

1. Sign up in the [Developer Portal](https://portal.openpayments.io/).
2. Create an application in **Sandbox** to get a `client_id` and `client_secret`. The secret is only
   shown once, so save it immediately.
3. Authentication is OAuth2 client credentials. This app requests the scope `aspspinformation corporate`,
   which is enough to list banks. Add `accountinformation` later when you start reading accounts.

### Zwapgrid

1. Log in to the [Client Portal](https://clients.zwapgrid.com).
2. Create an API key in the **Development** environment.
3. Authentication is a single `x-api-key` header. Every request also sends an `x-correlation-id` for tracing.
4. Reading ERP data requires a *consent* that your customer has accepted through the Onboarding Flow.
   The hello-world call only lists existing consents, so it works with an empty account too. See
   [Getting a Zwapgrid consent ID in sandbox](#getting-a-zwapgrid-consent-id-in-sandbox).

## Setup

```bash
git clone <this repo> nebula
cd nebula

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.template .env
```

Then open `.env` and fill in `OPEN_PAYMENTS_CLIENT_ID`, `OPEN_PAYMENTS_CLIENT_SECRET` and
`ZWAPGRID_API_KEY`. The remaining variables already default to the sandbox hosts.

`.env` is git-ignored. `.env.template` documents every variable and is the file you commit.

## Running

Call both sandboxes:

```bash
python -m nebula
```

Call one at a time:

```bash
python -m nebula open-payments
python -m nebula zwapgrid
```

Print the raw JSON responses instead of a summary:

```bash
python -m nebula --json
```

## Paying a supplier invoice end to end

`pay-invoice` runs the whole cycle for one invoice: it reads the invoice and the supplier's
payee account from Fortnox, initiates the payment in the bank, waits for the PSU to approve it
with BankID, and books the payment back onto the invoice so Fortnox marks it paid.

The three steps are separate systems, and only the last one is reversible, so run the dry run
first.

### 1. Create the invoice in Zwapgrid

```bash
python -m nebula zwapgrid_create_invoices --count 1 --supplier-id 2
```

`--supplier-id` has to be a supplier that already exists in Fortnox — list them with
`python -m nebula zwapgrid_suppliers`. The command continues the `INV-NNN` series and prints
the reference it created. Invoices cannot be deleted once created.

The supplier needs a payment account on its record in Fortnox (a bankgiro, plusgiro or IBAN
under `paymentMeans`). That is where the money will be sent; Fortnox does not keep payee
details on the invoice itself.

### 2. Fill in the payment settings

Payment initiation needs a PSU and a debtor account, which the read-only calls do not. Set
these in `.env`:

| Variable | What it is |
| --- | --- |
| `OPEN_PAYMENTS_BICFI` | The bank to send the payment to, e.g. `ESSESESS`. `python -m nebula open-payments` lists them. |
| `OPEN_PAYMENTS_PSU_ID` | The personal number of whoever approves the payment with BankID. |
| `OPEN_PAYMENTS_PSU_CORPORATE_ID` | The org. number of the company they act for. |
| `OPEN_PAYMENTS_DEBTOR_IBAN` | The account the money leaves. |

### 3. Check the payment before sending it

```bash
python -m nebula pay-invoice --invoice INV-007
```

This sends nothing. It resolves the invoice, the supplier and the payee account, then prints
the exact request body it would post to Open Payments. Check that the creditor account matches
the supplier you expect to pay — this is the point where a tampered payee account is visible.

The payment product is chosen from the payee account: a bankgiro or plusgiro (`SE:BG`, `SE:PG`)
goes out as `swedish-giro`, anything else as `domestic`.

### 4. Send it

```bash
python -m nebula pay-invoice --invoice INV-007 --send
```

This runs four calls in order, stopping at the first failure:

1. `POST /psd2/paymentinitiation/v1/payments/{product}` creates the payment. A
   `CREDITOR_ACCOUNT_FLAGGED` warning here means the creditor is on Svensk Handel's watchlist,
   and is printed rather than swallowed.
2. `POST .../authorisations` then `PUT .../authorisations/{id}` starts BankID. **Approve the
   payment in your banking app** — the command polls the SCA status until it is `finalised` or
   `failed`, and gives up after three minutes.
3. `GET .../status` confirms the bank did not reject it (`RJCT`).
4. `POST /supplierinvoices/{id}/payments` books the payment in Fortnox, using the Open Payments
   `paymentId` as the payment reference so the ledger entry points back at the bank payment.

The invoice is only booked in Fortnox after the bank has accepted the payment, so a failed
authorisation leaves the ledger untouched. `--bank-account` sets the asset account the payment
is booked against, defaulting to `1930`.

Money only moves against a real Fortnox company and a real bank account. Check which consent
the command prints before using `--send`.

## Invoices API

A small FastAPI app (`nebula/api.py`) exposes the mocked invoice endpoints behind the screens in
`index.html`. It's standalone and doesn't need `.env` or real credentials; the list is seeded from
`data/invoices.json` and the per-invoice details from `data/invoices/*.json`, into memory that
resets whenever the process restarts.

Every invoice in the list has a detail fixture, so every row opens and can be paid. The exception
is `2026-0431`, which starts on hold: paying it is refused until a verification call releases it.

### Running with Docker Compose

```bash
make up      # docker compose up --build -d
```

The API listens on `http://localhost:8000` (published on `127.0.0.1` only, so it isn't reachable
from other machines). Other commands:

```bash
make logs    # tail the api container logs
make down    # stop and remove the container
```

### Running without Docker

```bash
uvicorn nebula.api:app --reload --port 8000
```

### Endpoints

| Method | Path                          | Description                                                        |
| ------ | ----------------------------- | ------------------------------------------------------------------ |
| GET    | `/invoices`                   | List all mocked invoices. `hasDetail` says whether the row opens.   |
| GET    | `/invoices/{invoice_id}`      | Full detail for one invoice: bank details check findings and the actions it offers. |
| POST   | `/invoices/{invoice_id}/pay`  | Mark an invoice as paid. `404` if unknown, `409` if already paid or on hold. |
| POST   | `/invoices/{invoice_id}/verification-call` | Log a call to the supplier's register number and release the hold. `409` if the invoice is not on hold. |
| GET    | `/health`                     | Basic liveness check.                                              |

```bash
curl http://localhost:8000/invoices
curl -X POST http://localhost:8000/invoices/2026-0430/pay

# 2026-0431 is on hold: the call is what releases it, so both fields are required
curl -X POST http://localhost:8000/invoices/2026-0431/verification-call \
  -H 'Content-Type: application/json' \
  -d '{"contact":"Erik Nordvik, finance manager","note":"Confirmed the new account is theirs."}'
```

The call is recorded as a finding on the invoice, so the reason the hold was released stays
visible next to the check that raised it.

Interactive docs (Swagger UI) are served at `http://localhost:8000/docs`, and the OpenAPI schema
at `http://localhost:8000/openapi.json`. To try it in Postman without hand-building requests, use
*Import → Link* with the OpenAPI URL to generate a ready-made collection.

## Getting a Zwapgrid consent

A consent ID is created by you through the API, but it only becomes usable for reading data after
someone accepts it in the Onboarding Flow. There is no way to skip the browser step. Log in to
Fortnox in the same browser first, otherwise the flow falls back to Fortnox's signup and asks you
to pay.

This project targets **Fortnox** only. Commands select the most recently accepted consent whose
source is Fortnox, so a consent for any other accounting system is ignored rather than picked up
by accident.

```bash
python -m nebula create-consent
```

This creates the consent, generates a one-time code, and prints an Onboarding Flow URL:

```
Consent created: 541be958-c504-4bd5-ad2d-840761e039db

Open this URL in a browser to connect an accounting system:
  https://onboarding.zwapgrid.com/consent/541be958-.../fortnox/?otc=Vadsofpg%2FwXGWE...

Once accepted, run 'python -m nebula zwapgrid' - the accepted consent is
picked up automatically, so there is nothing to copy into .env.
```

Open the URL and complete the flow, then run `python -m nebula zwapgrid` again. The consent status
changes from `CREATED` to `ACCEPTED` and the accounting endpoints start returning data.

There is no consent ID in `.env`. The app queries the Consent API for consents with status
`Accepted` and uses the most recently created one, so onboarding a customer is the only step needed.
If several consents are accepted, the newest wins — worth knowing once you onboard more than one.

Options:

```bash
python -m nebula create-consent --name "Acme AB"     # consent display name
python -m nebula create-consent --system fortnox     # deep link to a specific system
python -m nebula create-consent --system any         # let the user pick the system
```

Two constraints worth knowing:

- The one-time code is **single use and expires after one hour**. Do not paste the URL anywhere that
  generates link previews (Slack, email) — the preview fetch consumes the code and the link dies.
  Re-run `create-consent` to get a fresh one.
- Development consents require a **Development** API key. A Production key returns `401` here.

Consent statuses are `0` CREATED, `1` ACCEPTED, `2` REVOKED, `3` INACTIVE. The API returns these as
numbers but only accepts the *names* when filtering (`?Status=Accepted`); passing `1` returns a `400`
despite what the docs say.

Expected output with working credentials:

```
=== Open Payments ===
Auth host: https://auth.sandbox.openbankingplatform.com
API host:  https://api.sandbox.openbankingplatform.com
Access token acquired for scope 'aspspinformation corporate'.
Found 4 bank(s) for SE:
  - ESSESESS  Skandinaviska Enskilda Banken AB
  - HANDSESS  Handelsbanken
  ...

=== Zwapgrid ===
Consents API:   https://apione.zwapgrid.com/consents
Accounting API: https://apione.zwapgrid.com/accounting
Found 1 consent(s), showing 1:
  - 8a179e27-...  Nebula sandbox consent (status=CREATED, source=None)

No accepted consent yet, so there is no ERP data to read. Run 'python -m nebula create-consent' and complete the Onboarding Flow.
```

The process exits with code `1` if either call fails, so it is safe to use in scripts.

## What each hello-world call does

| API | Call | Notes |
| --- | --- | --- |
| Open Payments | `POST /connect/token`, then `GET /psd2/aspspinformation/v1/aspsps` | Lists supported banks. Tokens are cached in memory and expire after one hour. |
| Zwapgrid | `GET /api/v1/consents` | Lists customer connections to accounting systems. |
| Zwapgrid | `GET /api/v1/consents?Status=Accepted` | Finds the consent to read data from. |
| Zwapgrid | `GET /api/v1/consents/{consentId}/companyinformation` | Skipped when no consent is accepted yet. |
| Zwapgrid | `POST /api/v1/consents`, then `POST /api/v1/consents/{consentId}/otc` | Run by `create-consent`. The consent ID comes from the `Location` header. |

## Troubleshooting

| Message | Cause |
| --- | --- |
| `Missing environment variables: ...` | `.env` has not been created or the values are blank. |
| `Open Payments returned 400 ... {"error":"invalid_client"}` | Wrong `client_id`/`client_secret`, or sandbox credentials used against the production host. |
| `Open Payments returned 403` | The token's scope does not cover the endpoint. Check `OPEN_PAYMENTS_SCOPE`. |
| `Zwapgrid returned 401` | Invalid API key, or a Production key used against Development consents. |

## Project layout

```
nebula/
  config.py          Environment variable loading and validation
  open_payments.py   OAuth2 client credentials + Open Payments REST calls
  zwapgrid.py        API key auth + Zwapgrid Consent and Accounting API calls
  cli.py             Command line interface
  api.py             FastAPI app with mocked get_invoices / pay_invoice endpoints
.env.template        Documented environment variables (committed)
.env                 Your local credentials (git-ignored)
Dockerfile           Container image for the invoices API
docker-compose.yml   Runs the invoices API via Docker Compose
Makefile             Shortcuts for docker compose (make up / logs / down)
```

## Next steps

To reach the actual goal — comparing bank data against ERP data — you will need to:

1. Request an Open Payments token with the `accountinformation` scope, create a PSU consent, and have
   the user authorise it at their bank. Sandbox test users are listed in the
   [credentials guide](https://docs.openpayments.io/docs/credentials).
2. Create a Zwapgrid consent and send your customer through the Onboarding Flow to connect their ERP.
3. Fetch accounts and balances from Open Payments and the matching accounting accounts and trial
   balances from Zwapgrid, then reconcile them on account number, currency, and period.
