# Security and Secrets

## Secret Classes

Treat all of the following as secrets:

- Login With Amazon client ID and client secret
- LWA refresh tokens
- LWA access tokens
- AWS access keys
- AWS secret access keys
- AWS session tokens
- SP-API authorization headers
- Request signatures
- Database credentials
- Odoo admin credentials
- API keys for AI providers
- Private keys
- Webhook signing secrets
- Environment variables containing credentials

## No Credentials in Git

Do not commit credentials, tokens, private keys, `.env` files with real values, production config exports, database dumps with secrets, or screenshots that expose secrets.

Use placeholders in documentation:

```text
LWA_CLIENT_ID=<redacted>
LWA_CLIENT_SECRET=<redacted>
LWA_REFRESH_TOKEN=<redacted>
AWS_ACCESS_KEY_ID=<redacted>
AWS_SECRET_ACCESS_KEY=<redacted>
```

## No Credentials in AI Prompts

Do not paste credentials into AI prompts, chat messages, issue descriptions, pull request descriptions, or generated documentation. Redact secrets before sharing logs or screenshots.

## Log Sanitization

Logs must not include:

- Passwords
- Tokens
- Client secrets
- AWS keys
- Authorization headers
- Signed request headers
- Full presigned report/feed document URLs
- Database passwords
- Private keys

Log Amazon request IDs, operation IDs, feed IDs, report IDs, and sanitized error categories where useful for debugging.

## Production Versus Staging

- Use staging or local mocked validation for development.
- Do not use production credentials in tests.
- Do not run live Amazon write calls from development unless explicitly authorized.
- Do not export stock or prices during validation.
- Do not import or mutate production settlements during validation.
- Do not post production accounting or reconcile production payouts during validation.

## Least Privilege

Grant only the roles required for the task:

- Amazon roles should match the API scope being used.
- Odoo users should have only the operational, inventory, accounting, or technical access needed.
- Accounting posting and reconciliation should remain restricted to authorized accounting users.
- Technical users should not use broad access to bypass business approval.

## Secret Rotation

Rotate secrets when:

- A token or key may have been exposed.
- A developer or service loses access rights.
- Amazon app credentials are regenerated.
- Staging credentials were accidentally used in production or vice versa.
- Logs, screenshots, or prompts accidentally included sensitive material.

Document rotation as an operational event without recording the secret values.

## Redacted Screenshots

Before sharing screenshots, redact:

- Tokens and keys
- Seller IDs and account IDs where not needed
- Email addresses and phone numbers where not needed
- Bank account data
- Customer personal data
- Presigned report/feed URLs

## Incident Handling

If a secret is exposed:

1. Stop using the exposed credential.
2. Rotate or revoke it in the owning system.
3. Remove it from logs, docs, issues, or chat where possible.
4. Check Git history before pushing.
5. Record what happened without storing the secret.

