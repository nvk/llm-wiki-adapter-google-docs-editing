# Google Docs Editing Adapter

Private, content-free llm-wiki tool for planning exact Google Docs edits,
applying them as tracked suggestions through the user's normal Chrome profile,
and verifying those suggestions through Google Docs API read-back.

- Repository: `nvk/llm-wiki-adapter-google-docs-editing`
- Manifest ID: `google-docs-editing`
- Protocol: `llm-wiki-adapter/v1`
- Version: `0.8.8`

The repository contains tools only. Document text, identifiers, OAuth
credentials, plans, receipts, journals, and connector messages remain in
external runtime storage or memory.

## Self-describing route

The private manifest declares the edit/revise/update/proofread/replace/suggest
route for `https://docs.google.com/document/d/` resources and points to
`AGENT_WORKFLOW.md`. A provider-neutral llm-wiki runtime can therefore discover
this adapter before URL ingestion without embedding Google authentication,
browser, planning, or recovery instructions in the public plugin.

Route discovery is content-free and does not echo or persist the target URL.
The adapter guide remains tool documentation; it contains no document content
or identifiers.

## Browser-connector design

v0.8 replaces the temporary loopback HTTP server, port, pairing code, bearer
token, content-script poller, and active-tab preparation with Chrome Native
Messaging.

The extension opens a persistent connection to the allowlisted user-level
native host. The host exposes a mode-0600 Unix socket under the adapter's
external state directory. An approved adapter run connects to that socket,
sends exactly one bounded job, and receives the governed mutation-boundary and
result messages. No localhost network listener is opened.

When a job arrives, the extension finds the exact approved document or opens it
in normal Chrome, focuses that tab, applies the plan, and returns a provisional
result. The side panel is status-only; no click is required. Chrome must already
be running and signed in to Google, but the user does not need to prepare a tab.

Version 0.8.8 also owns Find-and-replace preparation completely. It derives the
platform from Chrome rather than page JavaScript, brings the approved tab to the
front, retries trusted menu and documented-shortcut activation, and keeps the
verified dialog open across a multi-edit plan. The user must never pre-open the
dialog.

The extension has no `<all_urls>` or localhost host permission. Persistent host
access is limited to `https://docs.google.com/*`.

## Tracked-changes guarantee

Every successful `apply`:

1. is bound to the SHA-256 of the exact plan file;
2. requires llm-wiki's explicit `--approve-remote-write` hash;
3. confirms the planned Docs revision and accepted/rejected projections;
4. sends one in-memory job through Chrome's allowlisted native host;
5. opens or focuses only the exact approved document;
6. proves **Suggesting** mode before the first replacement and after each one;
7. records a private pending journal immediately before the first UI mutation;
8. discovers new suggestion IDs through Docs API read-back; and
9. proves the rejected projection is unchanged and the accepted projection
   equals the approved replacement plan.

UI completion alone is never success. A failed or partial write remains pending
so retry cannot silently duplicate suggestions.

## Google API authorization

Enable the Google Docs API, Google Drive API, and Google Picker API, then create
an OAuth **Desktop app** client. The adapter requests only
`https://www.googleapis.com/auth/drive.file`. No Workspace subscription or
Developer Preview enrollment is required.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python adapter.py configure-oauth \
  --client-secrets /absolute/private/downloaded-desktop-client.json
.venv/bin/python adapter.py auth \
  --document 'https://docs.google.com/document/d/<document-id>/edit'
```

`configure-oauth` validates and installs the owner-provisioned Desktop client
outside the repository. `auth` opens a local **Connect with Google** page, uses
PKCE and Google Picker, and stores the resulting token separately with mode
`0600`. Picker is required only when authorizing a genuinely new exact document.

Adapter registration and Google's per-file grant are separate checks. Confirm
live provider access without printing the document identifier or content:

```bash
.venv/bin/python adapter.py auth-status \
  --document 'https://docs.google.com/document/d/<document-id>/edit'
```

If this returns `picker_required: true`, a bounded user edit instruction already
authorizes the agent to start the pinned `auth --document` repair. The user must
complete Google's one-time provider interaction, but should not be asked for a
second llm-wiki permission or approval hash. Reauthorization preserves the
refresh token and union of previously granted file IDs.

## Install the normal-Chrome connector

Run once from the canonical private adapter checkout:

```bash
.venv/bin/python adapter.py browser-install
```

This installs an allowlisted Native Messaging host in Chrome's user-level
`NativeMessagingHosts` directory and prints the unpacked extension path. Then:

1. Open `chrome://extensions` in normal Chrome.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select the path printed by:

   ```bash
   .venv/bin/python adapter.py extension-path
   ```

The public key in `manifest.json` gives the unpacked extension a stable ID. The
native host accepts only that exact `chrome-extension://` origin. No pairing
code or port is used. Check the installation at any time:

```bash
.venv/bin/python adapter.py browser-status
```

`installed: true` proves the host manifest and executable. `connected: true`
means normal Chrome is running with the extension enabled. Reloading an
unpacked extension is needed only when installing a new extension version, not
for normal edits.

The default state root is
`~/.local/state/llm-wiki/google-docs-editing`; override it with
`LLM_WIKI_GOOGLE_DOCS_STATE_DIR`. Unix-domain socket paths have a small platform
limit, so a long external state path automatically uses a mode-0700,
user-and-state-specific short directory under `/tmp`; all durable state and the
native-host launcher remain in the configured external state root. On macOS, `browser-install` compiles that tiny content-free launcher with the system compiler so Chrome starts a native executable rather than a shell wrapper. A socket-only
override is available as `LLM_WIKI_GOOGLE_DOCS_NATIVE_SOCKET` for tests and
advanced installations.

## Register exact documents

Use llm-wiki v0.20.0 or newer:

```bash
/path/to/llm-wiki adapter add "$PWD" \
  --read-root /absolute/private/google-docs-input \
  --read-root /absolute/private/google-docs-output \
  --write-root /absolute/private/google-docs-output \
  --remote-resource 'google-docs:<document-id>' \
  --env LLM_WIKI_GOOGLE_DOCS_STATE_DIR \
  --env LLM_WIKI_GOOGLE_DOCS_NATIVE_SOCKET
/path/to/llm-wiki adapter doctor google-docs-editing --json
```

Each additional document requires another exact registered remote resource.
Normal adapter listings report only the count.

## Plan, approve, and apply

Create an edit spec under a registered external read root:

```json
{
  "schema": "google-docs-edit-spec/v1",
  "edits": [
    {
      "tab_id": "<tab-id>",
      "find": "synthetic old phrase",
      "replace": "synthetic replacement phrase"
    }
  ]
}
```

`tab_id` is optional only for a single-tab document. Each `find` must be unique
across the document. Unresolved suggestions elsewhere in the document are
preserved as the plan baseline, so additional non-overlapping suggestions can
be created without forcing the user to accept or reject earlier work. Planning
still fails when a requested edit overlaps text that already belongs to an
unresolved suggestion.

After `inspect` and `plan`, build a private apply request:

```bash
.venv/bin/python scripts/make_apply_request.py \
  --plan /absolute/private/google-docs-output/plan/plan.json \
  --output-dir /absolute/private/google-docs-output/apply-001 \
  --idempotency-key google-docs-apply-0001 \
  --request /absolute/private/google-docs-input/apply-001.json
```

Start the exact approved run:

```bash
/path/to/llm-wiki adapter run google-docs-editing \
  --request /absolute/private/google-docs-input/apply-001.json \
  --response /absolute/private/google-docs-output/apply-001-receipt.json \
  --approve-remote-write <exact-plan-sha256> \
  --json
```

The connector opens or focuses the exact document automatically. The complete
receipt stays private; terminal JSON remains content-free and identifier-free.

The extension uses Google's documented platform shortcut to enter Suggesting
mode, then falls back to the semantic mode menu. Find-and-replace controls are
resolved through Chrome's computed accessibility tree with a bounded semantic
DOM fallback. Exact values, unique-match state, and focus are rechecked before
the adapter authorizes the first Replace action. Accessibility data is handled
only in memory and never serialized.

v0.8 uses `google-docs-suggestion-plan/v4` and
`chrome-native-messaging-suggesting-ui`. Older extension-transport plans must be
replanned and re-approved; their hashes cannot be reused.

## Operations and limits

- `self-test`: local indexing and transport invariant check.
- `inspect`: private three-projection document inspection.
- `plan`: exact revision-locked replacement plan.
- `apply`: native-connector tracked suggestions plus API verification.
- `verify`: re-check a prior verified receipt.

v0.8 supports exact unique body-text replacements, including multiple tabs. It
does not generate arbitrary browser actions, raw Docs `batchUpdate` bodies,
header/footer edits, named ranges, or tab mutations.

## Primary platform references

- [Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)
- [Chrome Side Panel API](https://developer.chrome.com/docs/extensions/reference/api/sidePanel)
- [Chrome Debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger)
- [Chrome DevTools Protocol Accessibility domain](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/)
- [Extension service-worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)
- [Chrome Windows API](https://developer.chrome.com/docs/extensions/reference/api/windows)
- [Google Docs keyboard shortcuts](https://support.google.com/docs/answer/179738)
