# Google Docs editing agent workflow

This adapter owns Google-specific planning, native suggestion writes, revision
locking, idempotency, recovery, and read-back verification. The public llm-wiki
plugin only routes and enforces the approval boundary.

## Preferred transport: Google Docs API canary

Use the `api-*` operations, not the browser extension, when all of these are
true:

1. the Google Cloud project and account are enrolled in the Google Workspace
   Developer Preview Program;
2. the Docs API is enabled;
3. an already-issued OAuth access token is available only through
   `LLM_WIKI_GOOGLE_DOCS_ACCESS_TOKEN`; and
4. the requested file is within that token's authorized file set.

The canary deliberately does not mint or persist OAuth credentials. Prefer the
`drive.file` scope and an app-selected or app-created test file. Keep client
secrets, refresh tokens, access tokens, document IDs, plans, receipts, and API
responses outside this public repository.

Register the adapter with private roots, its API capability, and environment
variable names (not values):

```bash
/path/to/llm-wiki adapter add "$ADAPTER_ROOT" --replace \
  --read-root /absolute/private/google-docs-input \
  --read-root /absolute/private/google-docs-output \
  --write-root /absolute/private/google-docs-output \
  --remote-resource 'google-docs-api:authorized-files' \
  --env LLM_WIKI_GOOGLE_DOCS_ACCESS_TOKEN \
  --env LLM_WIKI_GOOGLE_DOCS_STATE_DIR
```

`adapter doctor google-docs-editing` is local and does not consume the token or
call Google. A read-only `api-inspect` is the first live enrollment and access
check.

## Fresh-session fast path

1. Route the exact Docs URL and read this guide.
2. Run `adapter doctor google-docs-editing` and stop on manifest drift.
3. Confirm the user has authorized this exact document and concrete edit.
4. For the first live preview test, use a disposable synthetic document.
5. Run the serialized API workflow once and wait for its final JSON.

For one exact replacement, the edit spec is:

```json
{
  "schema": "google-docs-edit-spec/v1",
  "edits": [
    {"find": "Synthetic old phrase.", "replace": "Synthetic new phrase."}
  ]
}
```

For a single-tab document, one append is also supported:

```json
{
  "schema": "google-docs-edit-spec/v1",
  "edits": [{"append": "Synthetic appended suggestion."}]
}
```

Create the edit spec in a registered private input root. Choose a new, empty
run directory under a registered private output root, then run:

```bash
"$ADAPTER_ROOT/scripts/run_api_suggestion_workflow.py" \
  --llm-wiki "$LLM_WIKI" --url "$DOC_URL" --edit-spec "$EDIT_SPEC" \
  --run-dir "$RUN_DIR" --idempotency-key "$IDEMPOTENCY_KEY" \
  --approve-remote-write
```

Do not launch overlapping operations, inspect partial artifacts while a stage
is running, or rerun `api-apply`. The runner serializes plan, apply, recovery,
and verify. It reuses the caller-stable idempotency key and never turns an
ambiguous result into a duplicate write.

Read-only inspection can be built separately:

```bash
"$ADAPTER_ROOT/scripts/make_api_inspect_request.py" \
  --url "$DOC_URL" --output-dir "$RUN_DIR" --request "$REQUEST"
```

The other request builders are `make_api_plan_request.py`,
`make_api_apply_request.py`, and `make_api_verify_request.py`.

## Governed API mutation

The API path fails closed unless every write has:

- an approved private plan and exact plan SHA-256;
- a caller-stable idempotency key;
- the raw Docs `revisionId` from planning as `requiredRevisionId`;
- `writeControl.writeMode` set to `SUGGEST`;
- `commentUpdateState` equal to `ALL_SAVED`;
- created suggestion IDs returned by the API; and
- a fresh API read-back proving the planned insertion/deletion text belongs to
  those open suggestion IDs.

Planning reads all document tabs with `SUGGESTIONS_INLINE`, resolves each exact
source once across all tabs, handles UTF-16 indexes, and rejects a source that
overlaps an existing suggestion. Canary replacements stay within one paragraph;
multiple replacements are sent in descending index order. Canary appends are
limited to single-tab documents so the target cannot be ambiguous.

The adapter writes a mode-0600 pending journal before crossing the HTTP mutation
boundary. A response timeout, preview partial failure, or failed read-back
blocks duplicate application. `api-recover` performs only a read and can issue
a receipt only when the original API response supplied exact suggestion IDs.
If the response was lost before those IDs were journaled, attribution remains
ambiguous: do not retry or automatically receipt the write.

Report only the content-free terminal status unless the user explicitly asks
to inspect private text artifacts.

## Boundaries

- A URL, OAuth token, API grant, or route match is not write authorization.
- Never put a bearer token in a request JSON, command line, plan, journal,
  receipt, log, or repository file.
- Do not use `EDIT`, omit `requiredRevisionId`, retry with a new idempotency key,
  or bypass the approved-plan hash.
- The native-suggestions API is Developer Preview. Do not treat it as a public
  production feature until Google makes it generally available and the adapter
  completes a live canary against an enrolled project.
- The repository is tool-only. Runtime content and identifiers stay in
  registered external private roots or memory.

## Legacy browser fallback

The original operations (`inspect`, `plan`, `apply`, `recover`, `verify`) still
use `browser-collaboration:active-tab` and `llm-wiki-chrome`. They are retained
only as an explicit fallback while the API canary is being proven. Do not choose
them merely because the Chrome extension happens to be installed.

If preview enrollment or API authorization is unavailable, stop and explain
that native tracked suggestions cannot yet use the API path. Do not silently
fall back to direct edits or browser automation.
