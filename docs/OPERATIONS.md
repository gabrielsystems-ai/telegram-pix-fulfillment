# Operations

Use a dedicated database and a separate bot for this installation. Work from the repository root. PostgreSQL is required by hosted entrypoints; SQLite is for local development only.

## Hosted process roles

After supplying your own PostgreSQL URL and credentials:

```powershell
uv run python -m vip.deployment migrate
uv run python -m vip.deployment check
uv run python -m vip.deployment api
```

Run the bot as a separate process:

```powershell
uv run python -m vip.deployment bot
```

`migrate` only applies this installation's Alembic head. `check` validates PostgreSQL/schema readiness without provider or Telegram calls. `api` runs one HTTP worker on `PORT` (default 8080) and disables access logs. `bot` acquires a PostgreSQL advisory lock and starts one poller with fulfillment, payment-notification reconciliation, and optional group/recovery workers. It performs real Telegram calls when started with valid credentials.

Direct `python -m vip.bot` and `uvicorn vip.api:create_app --factory` are development entrypoints; they do not provide the full hosted guard. Avoid duplicate pollers. To inspect matching local process command lines manually, the narrow PowerShell filter is:

```powershell
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'python(?:\.exe)?\s+-m\s+vip\.(?:bot\b|deployment\s+bot\b)'
}
```

Process command lines may contain sensitive operator arguments; do not export their output into the public repository.

## Webhooks and health

- Expose HTTPS `/webhooks/pushinpay` and configure the independent header secret on the provider side.
- Keep the bot worker running so inbox notifications can be reconciled and delivered.
- `/health` is process liveness. `/ready` checks the database migration head. Neither proves live payment delivery.
- Restrict trusted sales-server routes and protect credentials at ingress. The application does not provision host TLS, database roles or network access policy.

## Manual review

Never delete an uncertain charge or clear a delivery claim to force a retry. Compare authenticated provider evidence and local audit records first. Local expiry is not provider expiry. A confirmation received after expiry may be accepted for a known valid charge; replacements must not duplicate an earlier settled purchase.

A one-minute provider query budget is shared by workers and interactive status checks. Pending authenticated notifications take priority over unrelated interactive refreshes. The one-minute account budget still limits overall throughput; apply deployment ingress controls and monitor notification backlogs.

Pushin paid-to-canceled/expired transitions are logged for operator review; they are not a complete refund or chargeback implementation. Content-delivery uncertainty and financial reversals require an explicit operator policy.

## Optional packaging

```powershell
uv run python -m scripts.prepare_bundle --output <new-output-directory>
```

The bundler refuses an existing directory and copies only allowlisted source/configuration examples with SHA-256 hashes. It does not authenticate, upload, migrate or deploy. No private media is bundled. Runtime media must be mounted or registered separately.

The optional Dockerfile uses an editable source installation so module-relative migration discovery resolves to `/app/alembic`. It contains no `COPY . .`, secrets or bot startup default. Build/container execution must be checked in an environment with a working Docker engine.
