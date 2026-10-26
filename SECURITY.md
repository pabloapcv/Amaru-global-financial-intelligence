# Security

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the maintainer (pabloapcv on GitHub, or the contact address published with your deployment), not in a public issue. Include steps to reproduce and the impact you expect. We aim to acknowledge reports within 3 business days and to tell you how we will handle them.

## What ATLAS already does

**Accounts and sessions**
- Passwords are hashed with scrypt.
- Session tokens, API keys, invitations, share links and password-reset links are random and stored only as SHA-256 hashes.
- Sessions are HTTP-only cookies.
- Sign-in, two-factor and invitation attempts are throttled.

**Two-factor authentication**
- Optional TOTP, with single-use recovery codes.
- Seeds are encrypted at rest with `ATLAS_SECRET_KEY`.
- Codes cannot be replayed.
- Workspace owners can require it for everyone.

**Isolation**
- Every workspace's research, reports, portfolios, alerts, agents and keys are invisible to other workspaces.
- Removing a member ends their sessions and revokes their API keys immediately.

**Report pages**
- Shared and in-app report HTML is served with a sandboxing Content-Security-Policy and no scripts.
- Shared links expire, can be revoked, and their views are audited.

**Webhooks**
- Deliveries are signed (HMAC-SHA256 with a timestamp), and signing secrets are encrypted at rest.
- Endpoints must be HTTPS on public addresses, checked again at every send; redirects are not followed.

**Payments**
- Workspace plans change only through signed and timestamped Stripe webhooks, each applied once.

**Limits**
- Per-minute API rate limits and daily quotas by plan.
- Monthly Claude spending caps.

**Auditing**
- Owners and admins can review and export their workspace's activity log.
- The operator console needs an allow-listed email, a browser session and two-factor sign-in; it shows metadata only, and every plan change or suspension is audited.

**Personal data**
- People can download their data and delete their account.

**Backups**
- Restores verify checksums and reject unsafe archive entries.
- Backups never include the two-factor encryption key.

**Dependencies**
- The Security workflow audits Python and npm dependencies on every push and weekly.

## Operating ATLAS securely

- Set a long random `ATLAS_SECRET_KEY` and keep it stable and out of backups.
- Serve over HTTPS and set `ATLAS_COOKIE_SECURE=true`.
- Set `ATLAS_CORS_ORIGINS` to your web app's origin only.
- Protect `/metrics` with `ATLAS_METRICS_TOKEN`.
- Keep `ATLAS_ALLOW_SIGNUP=false` if accounts should come only from invitations.
- Run one scheduler. A database lease enforces this, but it is still worth configuring.
- With several API replicas, add a shared rate limiter in front: the built-in one is per process.
- Store backups off-site, encrypted, and test restores regularly.
