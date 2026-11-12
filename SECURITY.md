# Security

Do not include credentials, customer data, payment payloads, private media or runtime logs in issues. Report a vulnerability privately through GitHub's private vulnerability reporting if the repository owner enables it; otherwise contact the owner through their chosen private channel. No personal contact is embedded here.

The authenticated webhook token, sales API key, Telegram token and provider token grant different capabilities. Use independent generated secrets, keep them outside Git, and rotate any credential accidentally disclosed. The test suite uses synthetic identities and fake transports; passing it does not certify a live account or deployment.

Payment creation and delivery uncertainty are conservative by design. Webhook receipt does not itself confirm payment. Reconciliation must match the known transaction and frozen amount/currency before granting access.

Known operational limitations are documented in `docs/VERIFICATION.md`, including the shared provider query budget and manual reversal review. Network ingress limits, TLS, database access, backups and secret storage remain deployment responsibilities. No public deployment has been authorized or performed as part of this preparation.

The unused legacy browser checkout and web-purchase endpoints were removed. Old checkout/login environment variables cannot enable them. Trusted server API routes and the Telegram bot retain their existing authorization boundaries.
