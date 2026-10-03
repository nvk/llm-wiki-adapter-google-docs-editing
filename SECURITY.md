# Security

This public repository contains tool code only. Never commit real Google Docs
URLs or IDs, document text, edit specs, plans, receipts, journals, API
responses, OAuth client secrets, refresh tokens, bearer tokens, or generated
outputs.

The preferred path uses a Desktop app loopback callback with PKCE and Google's
Desktop Picker. Its OAuth request uses only the non-sensitive `drive.file`
scope, opts out of adding previously granted scopes, filters to Google Docs,
disables multiple selection, and can filter to one expected document ID. The
callback requires exactly one selected file and checks the expected ID before
exchanging and storing the authorization result.

Client configuration and refresh/access tokens are stored outside the
repository under `~/.config/llm-wiki/google-docs-editing/oauth/` by default.
Directories are mode 0700 and files are mode 0600; broader file permissions are
rejected. Token values never appear in status output, adapter requests, shell
arguments, plans, receipts, logs, or terminal errors. The optional
`LLM_WIKI_GOOGLE_DOCS_ACCESS_TOKEN` environment variable is an ephemeral test
override and must never be pasted into chat.

Every remote write requires native suggest mode, an approved plan hash, the
planned `requiredRevisionId`, a stable idempotency key, a pre-boundary private
journal, `ALL_SAVED` status, returned suggestion or comment IDs for every
planned effect, and API read-back. Read-back checks open suggestion threads,
person properties, comment content, assignees, quoted text, and anchor ranges.
A pending journal after an ambiguous response forbids a duplicate retry.

Runtime inputs and outputs belong in explicitly registered external private
roots. The adapter declares `writes_wiki: false` and never writes wiki content.
Report suspected vulnerabilities privately rather than opening an issue that
contains credentials, document identifiers, or content.

The registered adapter contract has no browser resource or browser operation.
If API authorization fails, the adapter stops rather than switching to a
weaker UI-automation path.
