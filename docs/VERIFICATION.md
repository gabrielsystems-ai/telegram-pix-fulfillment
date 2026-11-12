# Local release verification

Review date: 2026-11-11 (America/Sao_Paulo). Final local review after removing the unused legacy browser checkout. No GitHub publication, commit, deployment or live financial operation was performed.

## Results

| Check | Result |
| --- | --- |
| Complete test suite, Python 3.12 | **550 passed**, 1 dependency deprecation warning, 172.39 seconds |
| Affected API/payment/fulfillment tests | **96 passed**, including two new route-removal regression cases |
| Real PostgreSQL 18.6 integration | Passed migrations, concurrent single charge reservation, webhook replay deduplication and exactly one fake delivery |
| Network isolation | pytest-socket blocked external sockets; only loopback allowed; provider/Telegram transports were fake |
| Ruff lint | Passed |
| Ruff formatting | Passed |
| Lockfile consistency | Passed, 77 resolved packages |
| Dependency advisory audit | 77 installed distributions, zero known advisories reported |
| Build | Source distribution and wheel built successfully in temporary storage |
| Public source guard | No prohibited commercial references, recognized credential patterns, private host paths or forbidden publication artifacts detected |
| Lock URLs | Only PyPI/pythonhosted hosts; zero embedded URL credentials or absolute file URLs |
| Private-value comparison | Five nontrivial credential/identifier values compared in memory against the public authored tree; zero matches; values never printed or stored in the copy |
| Original filesystem integrity | All 9,100 original files retained identical SHA-256 digests; zero additions, removals or changed bytes |
| Independent Git | Initialized on `main`, no remotes and zero commits |

The generic local SQLite URL shared with a code default was classified as non-secret and excluded from the private-value comparison. No source environment file, database, media, backup, cache or Git history was copied.

The single test warning is an upstream Starlette notice that its httpx-based TestClient transport is deprecated. The tests pass; no unrelated HTTP transport migration was introduced for this release.

## What was preserved and adapted

The existing Python service architecture, SQL models, transaction constraints, migrations, payment checks, purchase-attempt logic, abandoned-Pix handling, consent checks, durable delivery and automated tests were retained.

Store/product labels, callback prefixes, bot identities, filenames, permission identifiers and schema checks were made generic. The public copy uses a fresh database lineage. Offer names and integer prices can be configured through a local catalog JSON file. Media defaults are disabled; tests fabricate transport bytes in OS temporary storage. Manual upload/preflight uses the configured bot username instead of a fixed commercial identity. Provider calls are disabled by default and the default selected Pushin endpoint is sandbox.

A source review found a shared-query-budget availability issue: an unpaid buyer's repeated status checks could acquire quota ahead of another buyer's paid notification. A regression test failed before the change. The copy now reserves priority for pending authenticated notifications before unrelated interactive refreshes, while retaining the atomic one-query-per-minute account gate. The new regression and existing shared-limit test passed, and an independent read-only re-review found the sustained starvation path resolved. One in-flight refresh racing a newly committed notification can still use the current minute's slot; notification failures can also consume capacity, so backlogs remain an operational concern.

## Removed legacy web surface

The browser checkout and `/web/purchases` bridge were wired to Omega independently of the selected Pushin provider and were not used by the bot/Pushin API. They were removed, together with their OIDC helper, HTML/CSS/JavaScript assets, browser QA fixture, obsolete configuration fields and exclusive PyJWT dependency. Its no-longer-needed transitive packages were removed from the lock/environment. No replacement checkout was added.

The removed features accounted for 43 former test cases; two new regression cases verify that all former browser/bridge routes are absent and return 404 even when old environment values are present, with both provider selections. They verify zero orders/payments/fulfillment jobs/web challenges are created and the health/products API remains available. The current full suite therefore contains 550 passing cases. Retained payment, fulfillment, bot and migration behavior remains covered.

Models and migration history, including the historical web-purchase table, remain unchanged. Shared Omega vendor services remain for existing bot/API compatibility, with the integration disabled by default; they do not expose a browser checkout. The final independent code review found no broken imports or unsafe bot/API drift. README, example environment, configuration, architecture, packaging and dependency notices now match the available interfaces.

Source distribution and wheel contents were checked for retired modules/assets; none were present. This review used a fresh disposable loopback PostgreSQL cluster, which was stopped after the complete suite.

## Security review scope

Static review covered application source, removal of the unused browser flow, migrations, scripts, tests, example configuration and publication files. It examined payment authenticity, identity binding, webhook authentication, replay behavior, fulfillment authorization, logging and secret exposure. No remaining source-backed payment/identity/authorization bypass was confirmed in that review. The availability issue described above was corrected and re-reviewed.

The public source guard checks authored/indexed files and, when present, every historical Git blob. Its optional `--denylist-file` accepts private terms from an external file; that list is deliberately not embedded in the public scanner. The release review supplied that external list and found no matches. This fresh repository has no commits, so no inherited history exists to scan. A broader private comparison checked the existing installation's credential/identifier values without transferring them into this repository. Ignored local dependencies and caches were generated independently; they are not publication inputs. The copied-source allowlist excludes operational state and private artifacts.

The dependency license inventory and installed LICENSE texts were reviewed. Project source is offered under MIT; upstream dependencies retain their respective terms, including LGPL/MPL obligations when redistributing bundled dependencies. See `THIRD_PARTY_NOTICES.md`. This review does not certify the author's ownership of future code or uploaded media.

No secret scanner or source audit can guarantee that all possible flaws are absent. Results describe the inspected local tree and exact checks above.

## Reproduce the PostgreSQL check

Create a fresh disposable PostgreSQL database with a name beginning `test_` on loopback. The test refuses remote hosts and does not connect unless explicitly configured. It applies migrations and writes synthetic data; use a new empty database for each invocation.

```powershell
$env:LOCAL_TEST_POSTGRES_URL = 'postgresql+psycopg://YOUR_LOCAL_TEST_USER:YOUR_LOCAL_TEST_PASSWORD@127.0.0.1:5432/test_fulfillment'
uv run pytest tests/test_postgres_integration.py --disable-socket --allow-hosts=127.0.0.1,::1 -q
Remove-Item Env:LOCAL_TEST_POSTGRES_URL
```

Without that variable, the PostgreSQL integration test skips and the rest of the suite runs locally with SQLite. CI declares a separate disposable PostgreSQL service and needs no repository secrets. The CI workflow has been prepared but has not run on GitHub.

## Explicit limits

- No live Telegram polling, charge creation, provider query, webhook registration, external database migration or deployment was performed.
- Real PostgreSQL was tested in a fresh temporary cluster bound only to loopback; the cluster was stopped after verification.
- Docker build/runtime was not tested because the local Docker engine was unavailable. The source layout is verified, and the Docker recipe uses an editable install to preserve migration paths.
- Wheel building is a packaging check, not standalone hosted-runtime verification. Use the repository/source checkout (with migrations and examples) for the documented application startup.
- No browser checkout or `/web/purchases` bridge is provided. Only the Telegram bot and authenticated server API are supported purchase interfaces.
- Provider-side support/configuration of `X-PushinPay-Token` must be verified for a new installation. The client does not provision that header.
- Automatic Pushin reconciliation and fulfillment run in the bot worker; API-only operation accepts signals without delivering them.
- The account-wide query budget limits throughput. Paid-to-canceled/expired Pushin transitions and uncertain delivery outcomes require operator review; a full automated refund/chargeback lifecycle is not implemented.
- Git has zero commits and no remote, so the owner can choose a public author identity and review the staged tree before the first commit/push.

GitHub publication instructions are in `docs/PUBLISHING.md`. Nothing in these results authorizes publication of an unreviewed tree or proves live provider readiness.
