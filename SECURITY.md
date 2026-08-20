# Security and content boundary

This public repository contains tool code only. Never commit real document
content, URLs, IDs, edit specs, plans, projections, receipts, journals,
credentials, captures, or results.

## Authorization

There are two separate gates:

1. each user extension click exposes one exact tab inside a bounded workspace;
   and
2. llm-wiki approves one exact plan hash, expected browser revision, and stable
   idempotency key.

The click authorizes bounded collaboration with that page, not an invented
edit or ambient browsing. The plan authorizes only its exact replacements or
one bounded append. The stable registered remote resource remains
`browser-collaboration:active-tab`;
the targeted adapter selects the exact requested document from the ephemeral
workspace at runtime.

## Browser boundary

The targeted adapter compiles a fixed Google Docs typed program for the shared
executor. No arbitrary JavaScript, `Runtime.evaluate`, natural-language browser
job, downloaded code, broad tab enumeration, or persistent host permission is
accepted. The exact collaboration ID, URL, origin, path, and tab are checked by
the executor before every action.

Page text and accessibility projections are private results. They never appear
in the extension panel, public terminal status, or repository. The extension
stores only ephemeral collaboration state and never stores document content,
plans, or receipts.

## Revision and tracked-suggestion proof

Planning hashes the ordered, bounded accessibility projection. Apply reruns the
same private inspection immediately before execution and fails closed if its
fingerprint differs from the approved revision. The executor does not use a
second full-tree hash because unrelated Docs UI and collaborator chrome can
change without changing the planned text. It instead clears and verifies every
dialog value and preflights every find as unique before authorization. Replace
clicks happen only after Suggesting mode is asserted, and the mode is asserted
again after the batch.

A successful UI command is provisional. The adapter requires a private
post-mutation projection, a changed revision hash, and visible planned text
for every planned edit before emitting a verified receipt. A journal written at
the boundary blocks duplicate retry after a partial or unverified write.

This browser-only proof is intentionally weaker than first-party Docs API
accepted/rejected projections and suggestion IDs. It trades that semantic depth
for zero provider OAuth and zero per-document grants. Failures are reported
honestly and never converted into direct edits.

## Threat boundary

The shared extension is powerful while attached to a user-exposed tab. Install
it only from the reviewed public executor repository. A compromised local
account, Chrome installation, extension, or already signed-in Google session is
outside this adapter's threat boundary.
