# Security and content boundary

This private repository contains tool code only. Never commit document content,
document identifiers, OAuth credentials, edit plans, API responses, receipts,
journals, screenshots, native messages, or generated results.

## Approval and automatic execution

An apply requires the exact llm-wiki plan hash, expected revision, stable
idempotency key, and exact registered remote resource. That plan-hash approval
is the write authorization. The extension receives only the plan's exact
replacements and never accepts caller-supplied browser programs.

## Browser boundary

The adapter never reads a Chrome profile, handles Google login credentials, or
enables a remote-debugging port. The installed Manifest V3 extension operates
in normal Chrome, opens or focuses only the exact approved Docs URL, and attaches
`chrome.debugger` only to that tab for one job. It detaches in `finally`.

Permissions are limited to `debugger`, `nativeMessaging`, `sidePanel`,
and `storage`. Host permissions are limited to
`https://docs.google.com/*`; `<all_urls>` and localhost hosts are forbidden. The
extension contains no remote scripts and persists no document ID, plan hash,
find text, replacement text, or result.

## Native Messaging boundary

`browser-install` writes Chrome's user-level native-host manifest with one exact
`allowed_origins` entry derived from the extension's committed public key. The
host verifies that caller again before opening a mode-0600 Unix socket under a
mode-0700 external state directory. There is no TCP listener, port, pairing code,
or bearer token.

Chrome starts the host over stdio. The host relays bounded JSON messages between
the connected extension and one same-user agent process at a time. It never logs
or stores those messages. Native and socket messages are size-capped. A second
concurrent edit is rejected.

The socket is local authentication by user and filesystem boundary, not an OS
sandbox. A compromised process running as the same user is outside this
adapter's threat boundary.

## Tracked-suggestion proof

The Docs API uses `drive.file` for revision-locked planning and three-projection
read-back. Immediately before the first Replace click, the extension requests
the adapter's governed mutation boundary. The adapter re-checks the revision,
accepted and rejected projections, and baseline suggestion IDs, then writes a
pending journal before authorizing the click.

A successful UI report is provisional. The adapter accepts success only after
new suggestion IDs appear, the rejected projection remains equal to the
pre-write document, and the accepted projection equals the approved plan. A
crash or partial write remains pending and blocks duplicate retry.

## Threat boundary

The extension is powerful while attached to an approved Docs tab, so install it
only from this reviewed private tool repository. A compromised local account,
compromised extension, malicious Chrome installation, or already-authorized
Google session is outside the adapter's security boundary.
