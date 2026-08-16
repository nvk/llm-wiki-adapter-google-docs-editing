# Security and content boundary

This private repository contains tool code only. Never commit document content,
document identifiers, OAuth credentials, pairing tokens, edit plans, API
responses, receipts, journals, screenshots, or generated results.

## Two independent approvals

An apply requires the exact llm-wiki plan hash, expected revision, stable
idempotency key, and exact registered remote resource. The extension then
requires a user click beside the active document. Neither approval can broaden
the plan: the adapter sends only the plan's exact replacements and the
extension does not accept caller-supplied browser programs.

## Browser boundary

The adapter never launches Chrome, reads a Chrome profile, handles Google login
credentials, or enables a remote-debugging port. The installed Manifest V3
extension operates in normal Chrome and attaches `chrome.debugger` only to the
active, exact approved Docs tab for the duration of one user-approved job. It
detaches in `finally` on success or failure.

Permissions are limited to `activeTab`, `debugger`, `sidePanel`, and `storage`.
Host permissions are limited to `https://docs.google.com/*` and
`http://127.0.0.1/*`; `<all_urls>` is forbidden. The extension contains no
remote scripts and renders bridge values with `textContent`, never `innerHTML`.

## Loopback bridge

Pairing and edit servers bind only to `127.0.0.1`. Pairing uses an eight-digit
single-session code with a five-attempt limit. Successful pairing creates a
random bearer token and binds it to the exact 32-character Chrome extension
origin. Both the token and origin are required for every job request, mutation
boundary, and result. Responses are `no-store`; request bodies are capped.

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
