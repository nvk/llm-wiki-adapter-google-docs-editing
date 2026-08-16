# Security and content boundary

This private repository contains tool code only. Never commit document content,
document identifiers, OAuth credentials, tokens, edit plans, API responses,
receipts, journals, or generated results.

Runtime API access uses the `drive.file` OAuth scope and exact document
resources registered in llm-wiki's mode-0600 adapter registry. Apply operations
require an exact approved plan hash and expected revision. The API is used for
planning and verification; mutation is performed only through the normal
Google Docs UI after its mode selector visibly confirms **Suggesting**. A write
is successful only when API read-back discovers new suggestion IDs and proves
the rejected and accepted projections.

The browser uses a dedicated external Chrome user-data directory. It never
reads or reuses the user's normal Chrome profile. The tracked
`custom-codex-google-docs` nono profile grants only the workspace plus Chrome's
Crashpad directory required by the system Chrome binary. Browser sign-in state,
history, caches, and cookies stay in the external runtime data plane.

Interactive authorization is served only on a random `127.0.0.1` port. The
one-click page uses a per-run local CSRF token, strict same-origin POST
validation, PKCE, OAuth state validation, no-store headers, a restrictive CSP,
and no remote scripts or assets. The adapter owner provisions the Desktop app
identity once into a mode-0600 machine-local profile; end users never upload
client credentials. User tokens are stored in a separate mode-0600 file and do
not duplicate the managed client secret. An optional document pin is passed to
Picker and independently checked on callback.

The browser driver fails before mutation when it cannot prove editor readiness,
Suggesting mode, or a deterministic unique replacement. It writes a pending
idempotency journal before the first replacement so a crash or partial run
cannot silently duplicate suggestions on retry. API projection checks remain
the final authority; UI success alone is never accepted as proof.
