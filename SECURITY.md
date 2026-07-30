# Security Policy

## Public Export Boundary

ScreenMemory is developed as a local-first research system. The public repository
is allowlist-based: only files that are safe for public release are committed.

Do not publish:
- API keys, tokens, OAuth files, cookies, browser profiles, or credential stores.
- Runtime logs, screenshots, local databases, vector stores, PID files, or caches.
- Operator-specific automation routes, private instructions, or live account data.
- Claims about metrics, benchmarks, sponsors, donations, or platform behavior
  unless the proof is published with the claim.

## Reporting

Report security issues privately to the repository owner through GitHub or the
contact channel listed on the owner profile. Do not open public issues with
credentials, exploit details, or private local paths.

## Maintainer Checklist

Before publishing a change:
- Run a secret scan over the files to be committed.
- Confirm `.gitignore` still blocks runtime and credential artifacts by default.
- Review README and docs for public claims that need proof.
- Keep the support link truthful: https://paypal.me/exzilcalanza is a support
  link, not evidence of donations, sponsorship, or endorsement.
