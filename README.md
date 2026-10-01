# Google Docs Editing Adapter

A governed Google Docs tracked-suggestions adapter for llm-wiki.

Version 0.12.1 is the complete local-first path: a persistent Desktop OAuth
flow with PKCE and automatic refresh, a Google Workspace add-on that grants
access to only the active document, and the governed Docs API suggestion
transport. It creates native Docs suggestions with
`writeControl.writeMode: SUGGEST`, locks every mutation to the planned
`requiredRevisionId`, journals an idempotency key before the HTTP boundary, and
verifies returned suggestion IDs with a fresh API read. The Chrome shared-tab
transport remains in the codebase only as an explicitly installed legacy
fallback.

- Repository: `nvk/llm-wiki-adapter-google-docs-editing` (public tool code)
- Manifest ID: `google-docs-editing`
- Protocol: `llm-wiki-adapter/v1`
- Version: `0.12.1`
- Runtime dependencies: Python standard library only

## What the user does

After one-time Google Cloud and OAuth setup:

1. Open the LLM Wiki side panel in a Google Doc.
2. Click **Share this document**. The add-on requests only `drive.file` access
   for that file.
3. Ask the agent: `wiki edit this Google Doc using suggestions: <URL> ...`.
4. Approve the concrete plan. The document receives native suggestions that
   can be accepted or rejected normally in Docs.

No Chrome extension, active-tab attachment, accessibility projection, or
browser focus is involved.

## Why the API path

The browser path depended on Google Docs UI structure, accessibility
projections, dialogs, focus, and an extension/native-messaging bridge. The API
path replaces those failure points with documented document indexes, revision
control, atomic batch updates, native suggestion IDs, and read-back.

Google made the Docs comments and suggestions API generally available on
September 30, 2026. The API transport no longer requires Developer Preview
enrollment.

## One-time setup

- One standard Google Cloud project with the Docs API enabled, an OAuth consent
  screen, and a **Desktop app** OAuth client.
- An Apps Script project linked to that same Cloud project for the in-Docs
  per-file grant add-on.

Follow **[Google setup and OAuth](SETUP.md)** from start to finish. It covers
the Cloud project, API enablement, consent audience and test user, exact
`drive.file` scope, Desktop-client download, local login, and private add-on
installation.

First install the API-only adapter. The legacy browser resource is omitted
unless `--with-browser-fallback` is explicitly supplied:

```bash
./scripts/install_local.py
```

After downloading the Desktop OAuth client JSON, pass its exact quoted path;
do not use a wildcard before the file exists. The consent flow opens a loopback
browser callback, uses PKCE, asks only for `drive.file`, and stores the refresh
token in a mode-0600 local file:

```bash
find "$HOME/Downloads" -maxdepth 1 -type f -name 'client_secret_*.json' -print
./scripts/google_docs_auth.py configure \
  "$HOME/Downloads/client_secret_ACTUAL_NAME.apps.googleusercontent.com.json"
./scripts/google_docs_auth.py login
./scripts/google_docs_auth.py status --json
```

The default private location is
`~/.config/llm-wiki/google-docs-editing/oauth/`. Override it with
`LLM_WIKI_GOOGLE_DOCS_OAUTH_DIR` when needed. An ephemeral token in
`LLM_WIKI_GOOGLE_DOCS_ACCESS_TOKEN` remains supported for testing and takes
precedence, but is no longer required.

Deploy the project in [`google_workspace_addon/`](google_workspace_addon/) as
a private Google Workspace add-on. Its Apps Script project must use the same
standard Cloud project as the Desktop client. The add-on contains no external
network calls and never reads document text; it only invokes Google's current
file-scope grant UI.

Google account consent and the Apps Script test deployment are provider-side
actions and cannot be preconfigured in this repository. Use a disposable
synthetic document for the first live test.

## Manual registration

Create a local virtual environment (there are no third-party packages):

```bash
python3 -m venv .venv
```

Register private roots, the API capability, and environment variable names:

```bash
/path/to/llm-wiki adapter add "$PWD" --replace \
  --read-root /absolute/private/google-docs-input \
  --read-root /absolute/private/google-docs-output \
  --write-root /absolute/private/google-docs-output \
  --remote-resource 'google-docs-api:authorized-files' \
  --env LLM_WIKI_GOOGLE_DOCS_ACCESS_TOKEN \
  --env LLM_WIKI_GOOGLE_DOCS_OAUTH_DIR \
  --env LLM_WIKI_GOOGLE_DOCS_STATE_DIR
```

Registration stores environment-variable names, never token values. Run
`adapter doctor google-docs-editing` after any manifest change.

## One serialized suggestion workflow

Save a private edit spec:

```json
{
  "schema": "google-docs-edit-spec/v1",
  "edits": [
    {"find": "Synthetic old phrase.", "replace": "Synthetic new phrase."}
  ]
}
```

Then run:

```bash
"$ADAPTER_ROOT/scripts/run_api_suggestion_workflow.py" \
  --llm-wiki "$LLM_WIKI" --url "$DOC_URL" --edit-spec "$EDIT_SPEC" \
  --run-dir "$RUN_DIR" --idempotency-key "$IDEMPOTENCY_KEY" \
  --approve-remote-write
```

The run directory must be absent or empty. The runner performs `api-plan`,
`api-apply`, optional non-mutating `api-recover`, and `api-verify` sequentially.
Private requests, plans, receipts, and verification artifacts stay in that
registered external directory. Terminal output is content-free.

Request builders are also available for controlled individual stages:

- `scripts/make_api_inspect_request.py`
- `scripts/make_api_plan_request.py`
- `scripts/make_api_apply_request.py`
- `scripts/make_api_verify_request.py`

## Safety properties

Every API mutation requires and verifies:

1. the exact approved plan hash;
2. the planned Docs `revisionId` as `requiredRevisionId`;
3. `writeMode: SUGGEST` (never a silent direct edit);
4. a stable local idempotency key and pre-boundary pending journal;
5. `commentUpdateState: ALL_SAVED`;
6. API-returned created suggestion IDs; and
7. fresh read-back of the planned text under those open suggestion IDs.

Exact replacements must have one source match across all tabs and cannot touch
an existing suggestion. Replacements stay within one paragraph. Index
calculations use UTF-16 code units. Multiple replacement ranges are applied
from the end backward. An append is accepted only when the document has one
tab.

If the HTTP result is ambiguous, the journal blocks resending. Recovery reads
the document and proves the exact API-returned suggestion IDs; it never
reapplies them. If no returned IDs reached the journal, recovery fails closed
for manual resolution rather than guessing which suggestion was created.

## Legacy browser operations

The earlier `inspect`, `plan`, `apply`, `recover`, and `verify` operations and
the `browser-collaboration:active-tab` capability remain intact for explicit
fallback testing. They still require `llm-wiki-chrome` 0.1.1 or later. The API
operations are separately named `api-inspect`, `api-plan`, `api-apply`,
`api-recover`, and `api-verify`, so transport choice cannot happen silently.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

All fixtures are synthetic. Tests use injected fake API and OAuth responses and
never need credentials, network access, or a real document. The Apps Script
manifest and source are also checked locally.

## Primary references

- [Google Docs API suggestions](https://developers.google.com/workspace/docs/api/how-tos/suggestions)
- [documents.batchUpdate](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
- [Docs API best practices](https://developers.google.com/workspace/docs/api/how-tos/best-practices)
- [Docs API authorization](https://developers.google.com/workspace/docs/api/auth)
- [OAuth for Desktop apps](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Editor file-scope actions](https://developers.google.com/workspace/add-ons/editors/gsao/editor-actions)
- [Docs API release notes](https://developers.google.com/workspace/docs/release-notes)
