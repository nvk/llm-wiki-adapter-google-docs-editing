# Google Docs Editing Adapter

A governed Google Docs tracked-suggestions adapter for llm-wiki.

Version 0.14.0 uses Google's official Desktop/Mobile Picker OAuth flow for
per-file access. A user selects one exact Google Doc in the system browser; the
same Desktop OAuth client then calls the Docs API directly. No Chrome extension,
Workspace add-on, Apps Script bridge, API key, service account, or broad Drive
scope is required.

Every mutation creates native Docs suggestions with
`writeControl.writeMode: SUGGEST`, locks the request to the planned
`requiredRevisionId`, journals a stable idempotency key before the HTTP boundary,
and verifies the returned suggestion IDs with a fresh API read.

- Repository: `nvk/llm-wiki-adapter-google-docs-editing` (public tool code)
- Manifest ID: `google-docs-editing`
- Protocol: `llm-wiki-adapter/v1`
- Version: `0.14.0`
- Runtime dependencies: Python standard library only

## User flow

After one-time Google Cloud setup:

1. Authorize the exact document:

   ```bash
   ./scripts/google_docs_auth.py authorize 'https://docs.google.com/document/d/DOCUMENT_ID/edit'
   ```

2. In Google Picker, select the displayed document and click **Insert**.
3. Ask the agent to edit that Google Docs URL using suggestions.
4. Review and explicitly approve the concrete edit plan.
5. Accept or reject the resulting native suggestions normally in Docs.

A document URL or Picker selection is not write approval.

## Why the API path

The old browser path depended on Google Docs UI structure, accessibility
projections, focus, dialogs, and an extension/native-messaging bridge. The API
path replaces those failure points with documented text indexes, revision
control, atomic batch updates, native suggestion IDs, and read-back.

Google made the Docs comments and suggestions API generally available on
September 30, 2026. The suggestion transport no longer requires Developer
Preview enrollment.

## Setup

Follow **[Google setup and OAuth](SETUP.md)**. The short version is:

1. Enable the Google Docs API and Google Picker API in one Cloud project.
2. Configure OAuth with only `https://www.googleapis.com/auth/drive.file`.
3. Create and download a **Desktop app** OAuth client.
4. Install and register the adapter:

   ```bash
   ./scripts/install_local.py
   ```

5. Import the exact downloaded client path and authorize a document:

   ```bash
   find "$HOME/Downloads" -maxdepth 1 -type f -name 'client_secret_*.json' -print
   ./scripts/google_docs_auth.py configure \
     "$HOME/Downloads/client_secret_ACTUAL_NAME.apps.googleusercontent.com.json"
   ./scripts/google_docs_auth.py authorize "$DOC_URL"
   ./scripts/google_docs_auth.py status --json
   ```

OAuth files are stored outside the repository under
`~/.config/llm-wiki/google-docs-editing/oauth/` with private permissions. An
`LLM_WIKI_GOOGLE_DOCS_ACCESS_TOKEN` environment override remains available for
short-lived testing.

## Manual registration

Create the standard-library-only virtual environment:

```bash
python3 -m venv .venv
```

Register private roots and the API capability:

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

Run `adapter doctor google-docs-editing` after any manifest change.

## Serialized suggestion workflow

Save a private edit spec:

```json
{
  "schema": "google-docs-edit-spec/v1",
  "edits": [
    {"find": "Synthetic old phrase.", "replace": "Synthetic new phrase."}
  ]
}
```

After the user approves that concrete change, run:

```bash
"$ADAPTER_ROOT/scripts/run_api_suggestion_workflow.py" \
  --llm-wiki "$LLM_WIKI" --url "$DOC_URL" --edit-spec "$EDIT_SPEC" \
  --run-dir "$RUN_DIR" --idempotency-key "$IDEMPOTENCY_KEY" \
  --approve-remote-write
```

The run directory must be absent or empty. The runner serializes `api-plan`,
`api-apply`, bounded non-mutating `api-recover` when needed, and `api-verify`.
Requests, plans, receipts, and verification artifacts remain in the registered
external private directory. Terminal output is content-free.

Individual request builders are also available:

- `scripts/make_api_inspect_request.py`
- `scripts/make_api_plan_request.py`
- `scripts/make_api_apply_request.py`
- `scripts/make_api_verify_request.py`

## Safety properties

Every API mutation requires and verifies:

1. the exact approved plan hash;
2. the planned Docs `revisionId` as `requiredRevisionId`;
3. `writeMode: SUGGEST`;
4. a stable idempotency key and pre-boundary private journal;
5. `commentUpdateState: ALL_SAVED`;
6. API-returned created suggestion IDs; and
7. fresh read-back under those open suggestion IDs.

Exact replacements must have one source match across all tabs and cannot touch
an existing suggestion. Replacements stay within one paragraph. Indexes use
UTF-16 code units. Multiple ranges are applied from the end backward. Appends
are accepted only for single-tab documents.

An ambiguous HTTP result is never resent. The private idempotency journal stays
beside the plan by default; `LLM_WIKI_GOOGLE_DOCS_STATE_DIR` can override that
location. Recovery can prove only exact suggestion IDs already returned by
Google; otherwise it fails closed for manual resolution.

## Legacy browser operations

The earlier `inspect`, `plan`, `apply`, `recover`, and `verify` operations remain
available only when the adapter is explicitly installed with
`--with-browser-fallback`. They require `llm-wiki-chrome` 0.1.1 or later. The
preferred operations are separately named `api-inspect`, `api-plan`,
`api-apply`, `api-recover`, and `api-verify`, so transport choice is never
silent.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

All fixtures are synthetic. Tests use injected API and OAuth responses and do
not need credentials, network access, or a real document.

## Primary references

- [Desktop and mobile Google Picker](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)
- [Drive `drive.file` scope](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Google Docs API suggestions](https://developers.google.com/workspace/docs/api/how-tos/suggestions)
- [`documents.batchUpdate`](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
- [OAuth for Desktop apps](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Docs API release notes](https://developers.google.com/workspace/docs/release-notes)
