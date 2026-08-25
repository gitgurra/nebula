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
.env.template        Documented environment variables (committed)
.env                 Your local credentials (git-ignored)
```

## Next steps

To reach the actual goal — comparing bank data against ERP data — you will need to:

1. Request an Open Payments token with the `accountinformation` scope, create a PSU consent, and have
   the user authorise it at their bank. Sandbox test users are listed in the
   [credentials guide](https://docs.openpayments.io/docs/credentials).
2. Create a Zwapgrid consent and send your customer through the Onboarding Flow to connect their ERP.
3. Fetch accounts and balances from Open Payments and the matching accounting accounts and trial
   balances from Zwapgrid, then reconcile them on account number, currency, and period.
