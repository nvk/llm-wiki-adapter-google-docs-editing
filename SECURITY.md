# Security and content boundary

This private repository contains tool code only. Never commit document content,
document identifiers, OAuth credentials, pairing tokens, edit plans, API
responses, receipts, journals, screenshots, or generated results.

## Approval and automatic execution

An apply requires the exact llm-wiki plan hash, expected revision, stable
idempotency key, and exact registered remote resource. That plan-hash approval
is the write authorization. The extension discovers the waiting job
automatically and cannot broaden it: the adapter sends only the plan's exact
replacements and never accepts caller-supplied browser programs.

## Browser boundary

The adapter never launches Chrome, reads a Chrome profile, handles Google login
credentials, or enables a remote-debugging port. The installed Manifest V3
extension operates in normal Chrome and attaches `chrome.debugger` only to the
exact approved Docs tab for the duration of one approved job. It
detaches in `finally` on success or failure.

Permissions are limited to `alarms`, `debugger`, `sidePanel`, and `storage`.
Host permissions are limited to `https://docs.google.com/*` and
`http://127.0.0.1/*`; `<all_urls>` is forbidden. The extension contains no
remote scripts and renders bridge values with `textContent`, never `innerHTML`.

## Loopback bridge

Pairing and edit servers bind only to `127.0.0.1`. Pairing uses an eight-digit
single-session code with a five-attempt limit. Successful pairing creates a
random bearer token and binds it to the exact 32-character Chrome extension
origin. Chrome omits `Origin` on extension GET requests, so read-only job fetches
authenticate with the bearer token; both the token and exact paired origin are
required for mutation-boundary and result POSTs. Responses are `no-store` and
request bodies are capped.

The token is the only persistent extension value besides the bridge port. The
extension does not persist the document ID, plan hash, find text, replacement
text, or result. Adapter-side pairing state and idempotency journals live in an
external mode-0700 directory with mode-0600 files.

## Tracked-suggestion proof

The Docs API uses `drive.file` for revision-locked planning and three-projection
read-back. Immediately before the first Replace click, the extension calls the
adapter's governed mutation boundary. The adapter re-checks the revision,
accepted and rejected projections, and baseline suggestion IDs, then writes a
pending journal before authorizing the click.

A successful UI report is provisional. The adapter accepts success only after
new suggestion IDs appear, the rejected projection remains equal to the
pre-write document, and the accepted projection equals the approved plan. A
crash or partial write remains pending and blocks duplicate retry.

## Threat boundary

The extension is powerful while attached to an active Docs tab, so install it
only from this private reviewed repository and keep it disabled when not in
use. A compromised local account, compromised extension, malicious Chrome
installation, or already-authorized Google session is outside the adapter's
security boundary. The loopback token is authentication, not an OS sandbox.
