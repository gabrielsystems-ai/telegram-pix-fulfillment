# telegram-pix-fulfillment

A Python application for Telegram purchases, Pushin Pay Pix payments and digital fulfillment backed by PostgreSQL. It includes a FastAPI backend, an aiogram bot, Alembic migrations and automated tests.

Payments, group automation and recovery messages are disabled by default. No bot credentials, customer data, private media or deployment configuration are included.

## Capabilities

- Create Pix charges with an explicit Pushin Pay sandbox or production configuration.
- Authenticate webhook signals with a separate header secret, then query the provider before accepting payment.
- Bind transactions to frozen order prices and buyer identities, with durable deduplication and purchase-attempt state.
- Deliver a registered digital video or a user-bound community invitation after confirmed payment.
- Preserve uncertain charge and delivery outcomes for reconciliation rather than replaying them automatically.
- Track abandoned purchases, consent, opt-out and optional recovery messages.
- Persist orders, grants, notifications and worker claims in PostgreSQL; use SQLite for isolated development.

The Python package remains named `vip` to preserve the existing application structure. The public distribution is `telegram-pix-fulfillment`; both `vip` and `pix-fulfillment` invoke its administrative CLI. This is an application template with fixed entitlement types, rather than a general marketplace SDK.

## Local setup

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/). Use a fresh checkout and separate test accounts.

```powershell
uv sync --locked --extra dev
Copy-Item .env.example .env
New-Item -ItemType Directory -Force var
uv run python -m alembic upgrade head
uv run uvicorn vip.api:create_app --factory --host 127.0.0.1 --port 8080 --no-access-log
```

The supplied example uses a local SQLite file and blank credentials. The API exposes `/health` and `/ready`; protected business routes fail closed until `SALES_API_KEY` is configured. This local setup does not start a Telegram poller or contact a payment provider. In a second terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/health
Invoke-RestMethod http://127.0.0.1:8080/ready
```

The checked-in lock file pins the tested dependency set. `uv sync` installs the project in editable mode; run commands from the repository root so migrations and example paths resolve correctly. No global installation or sibling project is required.

## Automated checks

```powershell
uv run pytest --disable-socket --allow-hosts=127.0.0.1,::1 -q
uv run ruff check .
uv run ruff format --check .
uv run python scripts/public_release_check.py
uv run pip-audit --skip-editable
```

The first command blocks external sockets while allowing the loopback transport used by ASGI tests. Provider requests use injected fake transports. Test media is fabricated in OS temporary storage and never copied from a real installation. See [verification](docs/VERIFICATION.md) for measured results and limitations, including the separate PostgreSQL integration command.

## Configure your own installation

Read [configuration](docs/CONFIGURATION.md) before enabling integrations. The supported primary purchase flow is inside Telegram:

1. Create a separate bot with BotFather and use a dedicated PostgreSQL database.
2. Set your own bot token, bot username, administrator IDs and private group ID. Keep them in your untracked `.env` or host secret store.
3. Edit [examples/catalog.json](examples/catalog.json), keeping product codes stable. Prices are illustrative integer BRL cents. Run catalog installation and record distribution approval for content you own.
4. Configure a video profile and register your own video if using video delivery. Media files are excluded from Git and container inputs.
5. Configure Pushin Pay, an HTTPS callback and its separate `X-PushinPay-Token` header. Verify that your provider setup sends this header; this client does not provision it.
6. Apply migrations and run the API and one bot process. The bot hosts payment reconciliation and delivery workers; the API alone only accepts webhook signals.

```powershell
# After editing .env with your own LOCAL or sandbox configuration:
uv run pix-fulfillment install-catalog
uv run pix-fulfillment review-product --code sample_video --rights-reference own-content-review
uv run pix-fulfillment review-product --code vip_permanent --rights-reference own-community-review
```

Catalog installation requires your configured negative `VIP_CHAT_ID`. Review commands record your decision; they do not establish legal ownership. Neither command confirms a payment or sends content. Product names, prices and entitlements are snapshotted into each order, so later catalog edits do not rewrite old orders.

The public application provides purchases through the Telegram bot and authenticated server API. It does not include a browser checkout, web-purchase bridge or Telegram OIDC login. The unused legacy web flow and its assets/configuration have been removed. See [architecture](docs/ARCHITECTURE.md).

## Operation and boundaries

See [operations](docs/OPERATIONS.md) for PostgreSQL roles, one-poller startup, webhook processing and uncertain outcomes. See [security](SECURITY.md) for reporting and [release review](docs/PUBLISHING.md) for creating an independent GitHub repository after local review.

This repository contains no automatic deployment workflow. The Dockerfile is an optional build recipe; it defaults to a configuration check and includes no credentials or private media. Container execution is verified separately from source tests.

Known operational constraints include the account-wide Pushin Pay query gate, manual review of reversals and uncertain delivery outcomes, and provider-side custom-header configuration. These are described in [verification](docs/VERIFICATION.md). This project does not establish live provider readiness by passing mocked tests.

## License

Project source is provided under the [MIT License](LICENSE). Dependencies retain their own licenses; consult [third-party notices](THIRD_PARTY_NOTICES.md), particularly LGPL-licensed Psycopg and MPL-licensed Certifi, before redistributing bundled binaries or images. No third-party library source or private media is vendored in this repository.
