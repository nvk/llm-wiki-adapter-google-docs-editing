# Google Docs Editing Adapter

Private, content-free llm-wiki tool for inspecting an explicitly exposed
Google Doc, planning exact replacements or a bounded append, applying them as
tracked suggestions, and verifying the browser-visible result.

- Repository: `nvk/llm-wiki-adapter-google-docs-editing` (private)
- Manifest ID: `google-docs-editing`
- Protocol: `llm-wiki-adapter/v1`
- Current released version: `0.8.8`
- This branch: local multi-tab collaboration development; not released

No Google OAuth client, Picker, Drive scope, Docs API token, Workspace account,
per-document Google grant, or persistent Docs host permission is used by this
development path.

## Architecture

The targeted adapter owns Google Docs semantics: exact replacement and append
planning, Suggesting-mode preparation, unique-match checks, revision
fingerprints, idempotency, and verification. The separate private
`llm-wiki-adapter-browser-execution` supplies only the shared typed executor.
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

Install the shared executor package into this adapter's private environment and
install its native host:

```bash
python3 -m venv .venv
.venv/bin/pip install --no-deps --no-build-isolation \
  /absolute/private/llm-wiki-adapter-browser-execution
/absolute/private/llm-wiki-adapter-browser-execution/.venv/bin/python \
  /absolute/private/llm-wiki-adapter-browser-execution/adapter.py browser-install
```

Use a normal local package install rather than an editable install: cloud-backed
workspaces can mark setuptools' editable `.pth` file hidden, causing Python to
skip it even though `pip` reported success. Reinstall the package after updating
the shared executor source until it has a packaged installer.

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
  --env LLM_WIKI_GOOGLE_DOCS_STATE_DIR \
  --env LLM_WIKI_BROWSER_EXECUTOR_NATIVE_SOCKET
```

This is adapter trust, not per-document authorization. Additional Docs need no
registration change. When the shared executor uses a custom private socket,
export `LLM_WIKI_BROWSER_EXECUTOR_NATIVE_SOCKET` before adapter runs; the
registry passes only its value and never stores it.

## Collaborate on a document

1. Open each page you want available to the current collaboration in normal
   Chrome and click **LLM Wiki Browser Executor** on that tab.
2. Give the agent the concrete edit instruction and exact document URL.
3. The adapter selects only that document from the explicitly shared workspace.

An inspect or plan request uses the static resource plus the expected URL:

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

Up to 15 non-overlapping find strings may be planned together, or one append
may be planned alone. Before the first
mutation, the extension recomputes the exact ordered accessibility projection
and compares it to the private plan revision hash. It then preflights every find
as `1 of 1`, enters Suggesting mode, crosses one governed mutation boundary,
applies the batch, proves Suggesting mode again, and returns a private read-back
projection. A verified receipt is emitted only when every planned text value is
browser-visible and the projection changed. A later `verify` binds the same
private plan to the receipt so volatile Docs chrome does not invalidate a
still-visible suggestion.

Use `scripts/make_apply_request.py` to build the governed apply request from the
private plan, then run it through llm-wiki with the exact plan hash. The terminal
response stays content-free; complete artifacts and receipts remain private.

## Limits

- Google Docs only; the exact requested document must be in the bounded set of
  explicitly shared tabs.
- Exact find/replace or one end-of-document append suggestion; no free-form
  browser programs.
- Up to 15 edits per plan.
- AX projection is the browser-owned revision model. It may fail closed when
  Google changes its accessibility surface or volatile UI changes the snapshot.
- Browser verification is not as semantically rich as Docs API accepted/rejected
  projections and suggestion IDs. The tradeoff removes provider OAuth and
  per-file grants while preserving an exact mutation boundary and read-back.

## Primary references

- [Chrome activeTab](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab)
- [Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)
- [Chrome Debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger)
- [Chrome DevTools Protocol Accessibility](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/)
- [Google Docs keyboard shortcuts](https://support.google.com/docs/answer/179738)
