# Google Docs Editing Adapter

Edit Google Docs with tracked suggestions from llm-wiki. The adapter plans
exact replacements or a bounded append, applies them in Suggesting mode, and
verifies the result.

- Repository: `nvk/llm-wiki-adapter-google-docs-editing` (public)
- Manifest ID: `google-docs-editing`
- Protocol: `llm-wiki-adapter/v1`
- Development version: `0.9.0` (unreleased)
- Shared executor requirement: `llm-wiki-chrome` `0.1.1` or later

No Google OAuth client, Picker, Drive scope, Docs API token, Workspace account,
per-document Google grant, or persistent Docs host permission is used by this
browser path.

## Architecture

The targeted adapter owns Google Docs semantics: exact replacement and append
planning, Suggesting-mode preparation, unique-match checks, revision
fingerprints, idempotency, and verification. The separate public-source
`llm-wiki-chrome` supplies only the shared typed executor.
It cannot accept natural-language tasks or arbitrary JavaScript.

Each extension click adds an ephemeral collaboration grant to a bounded
workspace of up to 16 explicitly shared tabs. The adapter selects the exact
requested Google document from that workspace; another shared tab being active
does not redirect the job. A grant rotates when its tab navigates, is revoked
on cross-origin navigation or tab close, and can be removed with **Stop** or
**Stop all**. Grants are bound to the exact tab, URL, origin, and window and are
held only in memory and Chrome session storage. The extension has `activeTab`,
not `<all_urls>` or a persistent `https://docs.google.com/*` permission.

## One-time setup

Install the stable shared native companion and its native host once:

```bash
brew install nvk/tap/llm-wiki-chrome
llm-wiki-chrome install
llm-wiki-chrome doctor
```

The adapter first uses an installed `llm-wiki-chrome` Python distribution in
its own environment. If there is none, it resolves the importable client root
from the stable `llm-wiki-chrome` command. This avoids editable-install `.pth`
files and session-specific source copies in cloud-backed workspaces.

Load the shared executor's `extension/` directory once from
`chrome://extensions` using **Load unpacked**. The Google adapter has no
provider-specific extension.

Register this adapter once with private input/output roots and one stable
remote capability:

```bash
/path/to/llm-wiki adapter add "$PWD" \
  --read-root /absolute/private/google-docs-input \
  --read-root /absolute/private/google-docs-output \
  --write-root /absolute/private/google-docs-output \
  --remote-resource 'browser-collaboration:active-tab' \
  --env LLM_WIKI_GOOGLE_DOCS_STATE_DIR
```

This is adapter trust, not per-document authorization. Additional Docs need no
registration change. Normal runs discover the private connector automatically.
Keep `LLM_WIKI_BROWSER_EXECUTOR_NATIVE_SOCKET` only for an explicit development
or sandbox override; the registry passes only its value and never stores it.

## Collaborate on a document

1. Open each page you want available to the current collaboration in normal
   Chrome and click **LLM Wiki for Chrome** on that tab.
2. Give the agent the concrete edit instruction and exact document URL.
3. The adapter selects only that document from the explicitly shared workspace.

Build requests in the registered private roots instead of hand-writing JSON:

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

The resulting inspect or plan request uses the static resource plus the exact
expected URL:

```json
{
  "protocol": "llm-wiki-adapter/v1",
  "adapter_id": "google-docs-editing",
  "operation": "plan",
  "arguments": {
    "collaboration_resource": "browser-collaboration:active-tab",
    "expected_document_url": "https://docs.google.com/document/d/SYNTHETIC_DOCUMENT/edit",
    "edit_spec": "/absolute/private/input/edit-spec.json"
  },
  "output_dir": "/absolute/private/output/plan",
  "options": {}
}
```

The example identifier is synthetic. Never commit a real URL, document ID,
plan, receipt, or extracted projection.

Edit specs are either exact replacements:

```json
{
  "schema": "google-docs-edit-spec/v1",
  "edits": [
    {"find": "synthetic old phrase", "replace": "synthetic replacement phrase"}
  ]
}
```

or one bounded append suggestion:

```json
{
  "schema": "google-docs-edit-spec/v1",
  "edits": [
    {"append": "synthetic appended suggestion"}
  ]
}
```

Up to 9 non-overlapping find strings may be planned together, or one append
may be planned alone. Inspection performs a bounded top-to-bottom AX scan and
restores the document cursor to the start. Its revision fingerprint covers the
Docs content projection rather than volatile editor chrome. Immediately before
the batch, the adapter reruns that inspection and requires the exact approved
revision fingerprint. Read-only inspection automatically retries up to two
transient CDP command failures before reporting a bounded error. The executor
then enters Suggesting mode, clears and
verifies each dialog value, waits for every find to settle as `1 of 1`, and crosses one governed mutation boundary,
applies the batch, proves Suggesting mode again, and returns a private read-back
projection. A verified receipt is emitted only when every planned text value is
browser-visible and the projection changed. A later `verify` binds the same
private plan to the receipt so volatile Docs chrome does not invalidate a
still-visible suggestion. When no state directory is configured, the
idempotency journal stays beside the private plan under `.google-docs-state/`.

Use `scripts/make_apply_request.py` to build the governed apply request from the
private plan, then run it through llm-wiki with the exact plan hash. The terminal
response stays content-free; complete artifacts and receipts remain private.

## Limits

- Google Docs only; the exact requested document must be in the bounded set of
  explicitly shared tabs.
- Exact find/replace or one end-of-document append suggestion; no free-form
  browser programs.
- Up to 9 edits per plan.
- AX projection is the browser-owned planning and read-back model. Inspection
  scans at most 20 viewports and 5,000 AX rows, so very large documents remain
  explicitly bounded rather than pretending to be exhaustively read.
- The adapter checks a content-only revision immediately before execution;
  exact replacements are also preflighted as a unique match in Docs before the
  governed mutation boundary.
- Browser verification is not as semantically rich as Docs API accepted/rejected
  projections and suggestion IDs. The tradeoff removes provider OAuth and
  per-file grants while preserving an exact mutation boundary and read-back.

## Primary references

- [Chrome activeTab](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab)
- [Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)
- [Chrome Debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger)
- [Chrome DevTools Protocol Accessibility](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/)
- [Google Docs keyboard shortcuts](https://support.google.com/docs/answer/179738)
