# Security and content boundary

This private repository contains tool code only. Never commit document content,
document identifiers, OAuth credentials, tokens, edit plans, API responses,
receipts, journals, or generated results.

Runtime access uses the `drive.file` OAuth scope and exact document resources
registered in llm-wiki's mode-0600 adapter registry. Apply operations require an
exact approved plan hash and use `requiredRevisionId` plus `writeMode: SUGGEST`.
A write is successful only when Google reports all suggestion threads saved and
read-back verification proves the rejected and accepted projections.

Interactive authorization is served only on a random `127.0.0.1` port. The
setup page uses a per-run local CSRF token, strict same-origin POST validation,
PKCE, OAuth state validation, no-store headers, a restrictive CSP, and no remote
scripts or assets. Uploaded Desktop client JSON is parsed in memory and is not
retained as a separate file. The client credentials required to refresh OAuth
are stored only in the mode-0600 token file. An optional document pin is passed
to Picker and independently checked on callback.

The Google suggestion-writing API is Developer Preview. Use it only with an
enrolled Google Workspace account and allowlisted Cloud project. Preview APIs
are not a production SLA.
