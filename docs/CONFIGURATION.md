# Configuration

Settings load process environment first, then `.env` in the working directory, then code defaults. Blank optional paths and URLs disable those settings. Start from `.env.example`; it contains no working credentials or real identifiers.

## Required resources

| Setting | Purpose | Safe example behavior |
| --- | --- | --- |
| `DATABASE_URL` | Dedicated PostgreSQL URL in hosted roles; SQLite for local checks | Local file from `.env.example` |
| `TELEGRAM_BOT_TOKEN` | Your independently created bot | Blank; bot cannot start |
| `TELEGRAM_BOT_USERNAME` | Expected username for manual upload and preflight | Blank; preflight/upload fail closed |
| `ADMIN_TELEGRAM_IDS` | Comma-separated positive IDs of your administrators | Blank; no administrator access |
| `SALES_API_KEY` | At least 32 characters for trusted server-to-server routes | Blank; protected API unavailable |
| `VIP_CHAT_ID` | Your negative private-supergroup ID | Blank; group automation/catalog setup unavailable |
| `SUPPORT_CONTACT` | Your public support description | Blank; no invented contact |

Use independently generated secrets, for example `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"`. Generate the sales and webhook secrets separately. Do not paste them into tracked files or shell scripts.

## Pushin Pay

| Setting | Meaning |
| --- | --- |
| `PAYMENT_PROVIDER=pushinpay` | Primary bot and server product-order provider |
| `PUSHINPAY_ENABLED=false` | Explicit financial opt-in; keep false for offline checks |
| `PUSHINPAY_TOKEN` | Token for your selected provider account/environment |
| `PUSHINPAY_BASE_URL` | Official sandbox default; only the official sandbox/production API roots are accepted |
| `PUSHINPAY_CALLBACK_URL` | Your HTTPS `/webhooks/pushinpay` endpoint; no credentials, query or fragment |
| `PUSHINPAY_WEBHOOK_TOKEN` | Independent secret of at least 32 characters |

The provider must send `X-PushinPay-Token` with the configured secret. Charge creation submits `value` and `webhook_url`, and does not install that header configuration. Without matching headers, callbacks are rejected. An accepted callback only stores a durable notification. A worker re-queries the known transaction and validates amount, currency and order before recording payment.

No automatic retry is performed after uncertain financial creation. Do not clear pending records and create another charge as a workaround. Provider credentials must match the selected sandbox/production API; neither endpoint is exercised by offline tests.

## Catalog and delivery

`CATALOG_PATH` optionally points to an operator-owned JSON file such as `examples/catalog.json`. All three stable codes must be present:

| Code | Permission | Behavior |
| --- | --- | --- |
| `sample_video` | `sample_video_access` | One-time registered-video access |
| `vip_permanent` | `vip_permanent_access` | Community access without an automatic expiry |
| `recovery_bundle` | Both permissions | Recovery-only offer subject to eligibility and approval |

Each entry contains only a nonempty `name` and integer `amount_minor` from 50 to 100,000,000 BRL cents. Keep codes stable after creating orders. These codes and permission types are deliberately fixed in services and migrations; adding a new entitlement type requires a code/schema change. Names and amounts are configurable without that change. Catalog synchronization is explicit: `install-catalog` updates all offers; `update-video-offer` updates only the video offer. Restart processes after changing configuration so imported services remain consistent.

`START_BANNER_PATH` and `START_VIDEO_PATH` are optional public menu media. Both default to disabled; the menu works with text. Provide absolute paths for mounted media. No example binary is distributed.

`VIDEO_PROFILE_PATH` points to a JSON profile for a video you own. Copy the structure from `examples/video-profile.json` and replace every placeholder using the actual SHA-256, byte size, width, height and duration in seconds. The sample profile is deliberately unusable. Register the video through the configured administrator's `/cadastrarvideo` reply workflow, or explicitly run `python -m scripts.upload_sample --video <absolute-path> --admin-id <your-admin-id>` after configuring the expected bot username. Upload is a real Telegram operation and is not part of offline verification. File IDs are cached per bot and profile hash; they are private runtime state.

The rights-review gate must be recorded for each product before purchase. The application does not supply content or verify your legal rights.

## Groups, abandoned Pix and recovery

`VIP_GROUP_ENABLED` defaults to false. Enable only with your own private supergroup and a bot with invite/restriction permissions. Invitations use join requests and are bound to the paid identity.

`ORDER_TTL_SECONDS` sets the local purchase window. A local expiry never proves provider expiry. Existing unpaid Pushin charges are re-queried and only resumed or replaced after validated provider state. Missing or ambiguous provider timestamps fail closed.

`RECOVERY_ENABLED` and `RECOVERY_PROMOTIONS_APPROVED` both default to false. Recovery also requires prior opt-in, an active reviewed recovery offer and fresh checks for prior purchases, pending charges, opt-out and blocked recipients. `RECOVERY_DELAY_SECONDS` is at least 1200. `RECOVERY_TEMPLATE` may override the neutral default; links are rejected. An uncertain send is not automatically replayed.

Timing defaults: linking token TTL 900 seconds, order TTL 1800, invite TTL 300, group synchronization interval 60. `BOT_TIMING_ENABLED` is false; enable only for safe performance diagnostics.

## Optional legacy interfaces

The public Omega payment vendor adapter is retained for compatibility with the inherited service architecture. It is distinct from any private store identity. It requires `OMEGA_PIX_ENABLED`, `OMEGA_CLIENT_ID`, `OMEGA_CLIENT_SECRET` and `OMEGA_CALLBACK_URL`; these remain blank/false and are unnecessary for the primary Pushin flow. Its default public API root is defined in Settings, and may be changed with `OMEGA_BASE_URL` to a trusted HTTPS vendor root.

No browser checkout or OIDC login is available in this public application. The obsolete web settings were removed from Settings and `.env.example`; leftover environment values cannot register the deleted routes. Never expose `SALES_API_KEY` to browser JavaScript. Linking tokens remain separate one-use capabilities for the existing bot/API flows.
