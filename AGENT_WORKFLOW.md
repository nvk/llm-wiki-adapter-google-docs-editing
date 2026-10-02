# Google Docs editing agent workflow

This adapter owns Google-specific planning, native suggestion writes, revision
locking, idempotency, recovery, and read-back verification. llm-wiki routes the
request and enforces the approval boundary.

## Preferred transport: Google Docs API

Use the `api-*` operations when:

1. the Docs API and Google Picker API are enabled for the Desktop OAuth project;
2. `scripts/google_docs_auth.py status --json` reports `connected: true` with
   only the `drive.file` scope; and
3. the user selected the requested file through
   `scripts/google_docs_auth.py authorize '<exact-doc-url>'`.

The authorization flow uses a loopback callback, PKCE, offline access, and
Google's Desktop Picker. The authorization URL is restricted to `drive.file`,
one Google Docs MIME type, one selection, and—when `authorize` is used—the
exact requested document ID. The callback verifies that same ID before token
storage. Keep credentials, document IDs, plans, receipts, and API responses out
of this public repository.

## Fresh-session fast path

1. Route the exact Docs URL and read this guide.
2. Run `adapter doctor google-docs-editing`; stop on manifest drift.
3. Run `scripts/google_docs_auth.py status --json` without reading OAuth files.
4. If disconnected, `reauthorization_required` is true, or the first API read
   returns 403, start the authorization helper yourself in a persistent shell:

   ```bash
   "$ADAPTER_ROOT/scripts/google_docs_auth.py" authorize \
     '<exact-doc-url>' --no-browser
   ```

   The process prints one short loopback URL, such as
   `http://127.0.0.1:43210/start`, and waits. Keep it running. Present only that
   short URL to the user as an `Authorize this Google Doc` link on its own line.
   Do not ask the user to change directories, paste a repository path, paste
   the Docs URL into a terminal, or copy the long provider OAuth URL. After the
   user completes Picker, wait for the process to finish and re-run the API
   read. The short link is local, single-session, and avoids terminal wrapping.

5. Run a read-only `api-inspect` or `api-plan` as the first live access check.
   A 403 means the file must be selected again through Picker.
6. Present the exact proposed edit and plan hash. A URL, route, token, or Picker
   selection is not write approval.
7. Only after explicit approval, run the serialized API apply/verify path.

For one exact replacement, the private edit spec is:

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

Create the edit spec in a registered private input root. Choose a new empty run
directory under a registered private output root. After explicit approval:

```bash
"$ADAPTER_ROOT/scripts/run_api_suggestion_workflow.py" \
  --llm-wiki "$LLM_WIKI" --url "$DOC_URL" --edit-spec "$EDIT_SPEC" \
  --run-dir "$RUN_DIR" --idempotency-key "$IDEMPOTENCY_KEY" \
  --approve-remote-write
```

Do not launch overlapping operations, inspect partial artifacts while a stage
is running, or rerun `api-apply`. The runner serializes plan, apply, recovery,
and verify. It reuses the stable idempotency key and never converts ambiguity
into a duplicate write.

A read-only request can be built separately:

```bash
"$ADAPTER_ROOT/scripts/make_api_inspect_request.py" \
  --url "$DOC_URL" --output-dir "$RUN_DIR" --request "$REQUEST"
```

## Governed API mutation

The API path fails closed unless every write has:

- an approved private plan and exact plan SHA-256;
- a caller-stable idempotency key;
- the planned Docs `revisionId` as `requiredRevisionId`;
- `writeControl.writeMode: SUGGEST`;
- `commentUpdateState: ALL_SAVED`;
- created suggestion IDs returned by Google; and
- fresh API read-back proving the planned text belongs to those open IDs.

Planning reads all tabs with `SUGGESTIONS_INLINE`, resolves each exact source
once, uses UTF-16 indexes, and rejects existing-suggestion overlap.
Replacements stay within one paragraph and execute in descending index order.
Appends are limited to single-tab documents.

A mode-0600 pending journal is written beside the private plan before crossing
the Docs API mutation boundary (or under `LLM_WIKI_GOOGLE_DOCS_STATE_DIR` when
explicitly configured). A timeout, partial failure, or failed read-back blocks
duplicate application. `api-recover` only reads and can receipt a write only
when the original response supplied exact suggestion IDs. Otherwise, do not
retry.

Report content-free terminal status unless the user explicitly asks to inspect
private artifacts.

## Boundaries

- Never treat a URL alone as authorization to write.
- Never put tokens in request JSON, command lines, plans, journals, receipts,
  logs, repository files, or chat.
- Never inspect or report stored OAuth client/token files. Status is the safe
  interface.
- Never hand the user a long authorization command. Launch `authorize
  --no-browser` yourself and expose only its short loopback start link.
- Never use `EDIT`, omit the required revision, bypass the approved plan hash,
  or retry with a new idempotency key.
- Runtime content and identifiers stay in registered external private roots or
  memory; the repository is tool-only.

## No browser fallback

The registered adapter exposes only `api-*` operations and the
`google-docs-api:authorized-files` resource. Never ask the user to share a tab
or install/open the Browser Executor extension. If API authorization is
unavailable, explain the Picker prerequisite instead of changing transports.
