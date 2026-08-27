# Google Docs editing agent workflow

This provider guide is owned by the targeted `google-docs-editing` adapter. The
public llm-wiki plugin discovers it through `adapter route`; Google-specific
steps do not belong in llm-wiki itself.

## Fresh-session fast path

For a normal new agent session, do only this before provider work:

1. Resolve the bundled `llm-wiki` CLI from the active wiki skill.
2. Run `adapter route` for the exact Docs URL and requested intent.
3. Read this guide and run `adapter doctor google-docs-editing`.
4. Build each v1 request in a registered private input root and run it with
   `adapter run`; put its output and response in the registered private output
   root.

Use the executable adapter-owned request builders rather than hand-writing JSON
or assuming a `python` command exists:

```bash
"$ADAPTER_ROOT/scripts/make_inspect_request.py" \
  --url "$DOC_URL" --output-dir "$RUN_DIR" --request "$REQUEST"

"$ADAPTER_ROOT/scripts/make_plan_request.py" \
  --url "$DOC_URL" --edit-spec "$EDIT_SPEC" \
  --output-dir "$RUN_DIR" --request "$REQUEST"

"$ADAPTER_ROOT/scripts/make_apply_request.py" \
  --plan "$PLAN" --idempotency-key "$IDEMPOTENCY_KEY" \
  --output-dir "$RUN_DIR" --request "$REQUEST"

"$ADAPTER_ROOT/scripts/make_verify_request.py" \
  --plan "$PLAN" --receipt "$RECEIPT" \
  --output-dir "$RUN_DIR" --request "$REQUEST"
```

For an authorized write with a finished edit spec, prefer the serialized
workflow runner so plan, apply, and verify cannot overlap:

For one exact append, the complete edit-spec shape is:

```json
{"schema":"google-docs-edit-spec/v1","edits":[{"append":"Exact text."}]}
```

Create that file under the registered private input root. Choose a new run
directory under the registered private output root; it may be absent or already
created and empty, but it must not contain artifacts from another attempt.

```bash
"$ADAPTER_ROOT/scripts/run_suggestion_workflow.py" \
  --llm-wiki "$LLM_WIKI" --url "$DOC_URL" --edit-spec "$EDIT_SPEC" \
  --run-dir "$RUN_DIR" --idempotency-key "$IDEMPOTENCY_KEY" \
  --approve-remote-write
```

Run only that command and wait for its final JSON. If the shell tool reports a
running process or session ID, poll that same process with the tool's wait
primitive until it exits. Never inspect partial artifacts, rerun planning, or
launch another adapter operation while any adapter command is still running.
Do not inspect helper source, `--help`, or the README when this guide already
supplies the exact command.

The runner handles an ambiguous append read-back by checking the pending
journal and using the adapter's non-duplicating exact-text recovery operation.
Do not manually rerun `apply`, even with the same idempotency key.

Do not start by inspecting extension source, Homebrew files, sockets, browser
internals, or general wiki articles. The registered adapter is the supported
entry point and owns runtime bootstrap. For a read-only request, stop after a
successful `inspect` and report the adapter version, stable revision, and
bounded counts without printing private document text.

## User flow

1. The user opens each page they want to share in their normal signed-in Chrome.
2. The user clicks **LLM Wiki for Chrome** on each such tab. Every gesture
   adds an ephemeral grant bound to the exact tab, URL, origin, and window. The
   workspace is capped at 16 grants.
3. The agent uses this adapter. There is no Google OAuth, Picker,
   per-document API grant, persistent host permission, or per-document
   llm-wiki registration.

The adapter and the shared browser executor are still installed and trusted
once. Register only the stable capability
`browser-collaboration:active-tab`; the historical capability name remains
stable while its runtime workspace supplies explicitly shared tabs. The
adapter selects the requested Google document by its exact document identity.

## Boundaries

- The repository is a content-free tool. Runtime URLs, document IDs, text,
  edit specs, plans, receipts, journals, and accessibility projections stay in
  registered private roots or memory.
- A URL or collaboration click alone is not permission to invent changes. The
  user must give a concrete edit instruction.
- Only exact find/replace suggestions or one bounded end-of-document append are
  accepted. No arbitrary JavaScript, browser program, Docs API mutation, or
  silent direct edit is available.
- Every write requires the exact approved plan hash, browser revision
  fingerprint, stable idempotency key, one governed mutation boundary, and
  browser read-back verification.

## Route and health

1. Run `adapter route --intent edit --resource '<document-url>' --json`.
2. Run `adapter doctor google-docs-editing --json` and stop on manifest drift.
3. Confirm the requested Google document appears in the explicitly shared
   workspace. The adapter selects and enforces the exact document again; the
   currently active tab is not authoritative.
4. If the document is not shared, tell the user only: open that exact Doc and
   click the shared executor extension. Do not open OAuth or Picker.

`adapter doctor google-docs-editing` is the fresh-session health check. Do not
substitute a `llm-wiki-chrome` executable found on `PATH`: a separate
development or Homebrew install can describe its own packaged files rather
than the extension currently loaded by Chrome. An `invalid-program` result from
the registered adapter is the actionable version-mismatch signal.

## Governed edit

1. Run `inspect` with the static collaboration resource and the requested URL
   when document text is needed to design exact replacements. If the user
   already supplied one exact append, go directly to `plan`; planning performs
   its own bounded inspection.
2. Build the smallest `google-docs-edit-spec/v1` plan: up to 9 non-overlapping
   `find`/`replace` suggestions, or one `append` suggestion when no safe
   non-overlapping source text exists.
3. Run `plan`. It binds the plan to the selected collaboration ID, exact live
   URL, document ID, and revision fingerprint.
4. Pass the plan's hash internally through `--approve-remote-write`, use a
   caller-stable idempotency key, and pass the plan revision as
   `expected_revision`. Never ask the user to copy an approval hash.
5. Run `apply`. The adapter first repeats private inspection and requires the
   approved revision. The executor then enters Suggesting mode, clears and
   verifies each dialog field, waits for every find to settle as `1 of 1`, or positions an
   append at the exact document end, applies the plan, proves Suggesting mode
   again, and returns a private post-mutation projection.
6. Treat success as verified only when the adapter observes every planned text
   value in browser read-back or, for one appended suggestion that Docs exposes
   only in a truncated suggestion card, proves the exact text through Docs'
   Find dialog and emits a verified remote receipt. A pending journal after a
   post-boundary failure blocks duplicate retries; `recover` may prove that
   exact append without sending it again.
7. `verify` re-checks the exact exposed document against the private plan and
   receipt. It tolerates volatile Docs UI projection changes only when every
   planned text value remains browser-visible.

Report content-free status and counts unless the user explicitly asks to see
document text from the private inspection artifact.

The adapter itself retries a transient `cdp-command-failed` or
`cdp-command-timeout` up to four times during read-only inspection and during a
suggestion program only while it remains before the governed mutation boundary.
If it still returns an error, stop and report the bounded action and error; do
not add an outer retry or turn a normal user request into a source-code or
package-manager audit. For any write failure at or after the governed mutation
boundary, never retry with a new idempotency key. Diagnose the journal and
receipt state first.

Never switch to ad hoc low-level browser calls, ordinal comment controls, or
manual find/replace loops. That bypasses the plan, revision, idempotency, and
read-back controls and can leave a partially applied document.

## Session reliability

- A new agent session does not require another extension click while Chrome is
  still running and the tab grant remains in extension session storage.
- A browser restart, extension reload/update, tab close, cross-origin
  navigation, or explicit **Stop** revokes the ephemeral grant by design.
- Normal runs discover the private native connector automatically. Do not pin a
  per-process `s.<instance>` socket in adapter registration or shell startup.
- If runtime loading fails, use the registered adapter error first. Inspect the
  companion executable selected by the adapter only when that error explicitly
  reports a missing or incompatible shared executor.
