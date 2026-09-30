# Google Docs Editing Adapter

A governed Google Docs tracked-suggestions adapter for llm-wiki.

Version 0.10.0 adds a **Google Docs API Developer Preview canary** as the
preferred transport. It creates native Docs suggestions with
`writeControl.writeMode: SUGGEST`, locks every mutation to the planned
`requiredRevisionId`, journals an idempotency key before the HTTP boundary, and
verifies returned suggestion IDs with a fresh API read. The Chrome shared-tab
transport remains available only as a legacy fallback.

- Repository: `nvk/llm-wiki-adapter-google-docs-editing` (public tool code)
- Manifest ID: `google-docs-editing`
- Protocol: `llm-wiki-adapter/v1`
- Version: `0.10.0`
- Runtime dependencies: Python standard library only

## Why the API path

The browser path depended on Google Docs UI structure, accessibility
projections, dialogs, focus, and an extension/native-messaging bridge. The API
path replaces those failure points with documented document indexes, revision
control, atomic batch updates, native suggestion IDs, and read-back.

The preview API is still pre-GA. This branch is a canary, not a claim that the
transport is ready for public production use.

## Canary prerequisites

- Enrollment in the Google Workspace Developer Preview Program.
- A Google Cloud project with the Docs API enabled.
- An OAuth token authorized for the test file. Prefer `drive.file` and an
  app-selected or app-created disposable document.
- The token supplied at runtime as `LLM_WIKI_GOOGLE_DOCS_ACCESS_TOKEN`.

The adapter intentionally does not implement an OAuth consent flow or persist a
refresh token yet. That keeps the first canary small and lets the live API
contract be proven before building an optional Docs sidebar or Picker flow.
Never commit or pass bearer tokens on the command line.

## Registration

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
an existing suggestion. Canary replacements stay within one paragraph. Index
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

All fixtures are synthetic. Tests use an injected fake API client and never
need OAuth, network access, or a real document.

## Primary references

- [Google Docs API suggestions](https://developers.google.com/workspace/docs/api/how-tos/suggestions)
- [documents.batchUpdate](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
- [Docs API best practices](https://developers.google.com/workspace/docs/api/how-tos/best-practices)
- [Docs API authorization](https://developers.google.com/workspace/docs/api/auth)
- [Google Workspace Developer Preview](https://developers.google.com/workspace/preview)
