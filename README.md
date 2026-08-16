# Google Docs Editing Adapter

Private, content-free llm-wiki tool for planning exact Google Docs edits,
applying them as tracked suggestions through a normal-Chrome extension, and
verifying the suggestions through Google Docs API read-back.

- Repository: `nvk/llm-wiki-adapter-google-docs-editing`
- Manifest ID: `google-docs-editing`
- Protocol: `llm-wiki-adapter/v1`
- Version: `0.7.1`

The repository contains tools only. Document text, identifiers, OAuth
credentials, pairing tokens, plans, receipts, and journals remain in external
runtime directories.

## Why an extension

Google rejects automated browser sign-in, while a second Chrome instance can
also conflict with an outer OS sandbox. v0.7 does not launch or sign in to
Chrome. Its Manifest V3 extension runs in the user's existing normal Chrome
profile. After the user approves the exact plan hash through llm-wiki, the
extension automatically finds the matching open document and applies the plan.

The extension uses Chrome's side-panel API for one-time pairing and the
debugger API for trusted mouse and keyboard input in the exact Google Docs tab.
It has no
`<all_urls>` permission. Persistent host access is limited to
`docs.google.com` and the loopback bridge at `127.0.0.1`.

## Tracked-changes guarantee

Every successful `apply`:

1. is bound to the SHA-256 of the exact plan file;
2. requires llm-wiki's explicit `--approve-remote-write` hash;
3. confirms the planned Docs revision and accepted/rejected projections;
4. sends one in-memory job over a paired, bearer-authenticated loopback bridge;
5. is discovered automatically by a scoped Docs-tab poller or 30-second alarm;
6. requires the exact approved document to be open in Chrome;
7. proves **Suggesting** mode before the first replacement and after each one;
8. records a private pending journal immediately before the first UI mutation;
9. discovers new suggestion IDs through Docs API read-back; and
10. proves the rejected projection is unchanged and the accepted projection
    equals the approved replacement plan.

UI completion alone is never success. A failed or partial write remains
pending so retry cannot silently duplicate suggestions.

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
`0600`.

## Install and pair the Chrome extension

1. In normal Chrome, open `chrome://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked** and select the path printed by:

   ```bash
   .venv/bin/python adapter.py extension-path
   ```

4. Click the extension icon to open its side panel.
5. Start the one-time local pairing server:

   ```bash
   .venv/bin/python adapter.py extension-pair
   ```

6. Enter the printed eight-digit code in the side panel and click **Pair**.

Pairing binds a random bearer token to that extension's exact
`chrome-extension://` origin. Chrome stores the token in extension-local
storage; the adapter stores it in the external state directory with mode
`0600`. Pairing and edit servers bind only to `127.0.0.1`.

The default bridge port is `17843`. Override both pairing and apply with
`LLM_WIKI_GOOGLE_DOCS_EXTENSION_PORT`, and enter the same port in the side
panel. The default state root is
`~/.local/state/llm-wiki/google-docs-editing`; override it with
`LLM_WIKI_GOOGLE_DOCS_STATE_DIR`. Any override used through llm-wiki must be
allowlisted with `adapter add --env`.

## Register exact documents

Use llm-wiki v0.20.0 or newer:

```bash
/path/to/llm-wiki adapter add "$PWD" \
  --read-root /absolute/private/google-docs-input \
  --read-root /absolute/private/google-docs-output \
  --write-root /absolute/private/google-docs-output \
  --remote-resource 'google-docs:<document-id>' \
  --env LLM_WIKI_GOOGLE_DOCS_STATE_DIR \
  --env LLM_WIKI_GOOGLE_DOCS_EXTENSION_PORT
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
across the document. Planning is refused while unresolved suggestions exist.

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

While it waits, keep the exact Google Doc open in normal Chrome. The paired
extension discovers the approved job and creates the suggestions without a
second extension interaction. The complete receipt stays private; terminal
JSON remains content-free and identifier-free.

The extension discovers the current Find-and-replace dialog by semantic field
and control names rather than depending on one Google Docs CSS class. It falls
back to Google's documented platform shortcut when the Edit-menu item is not
available.

Changing from v0.5's browser driver to the v0.6 extension changes the plan
schema and transport. Old `google-docs-suggestion-plan/v2` plans must be
replanned and explicitly re-approved as v3; their old hashes cannot be reused.

## Operations and limits

- `self-test`: local indexing and transport invariant check.
- `inspect`: private three-projection document inspection.
- `plan`: exact revision-locked replacement plan.
- `apply`: extension-driven tracked suggestions plus API verification.
- `verify`: re-check a prior verified receipt.

v0.7 supports exact unique body-text replacements, including multiple tabs. It
does not generate arbitrary browser actions, raw Docs `batchUpdate` bodies,
header/footer edits, named ranges, or tab mutations.

## Primary platform references

- [Chrome Side Panel API](https://developer.chrome.com/docs/extensions/reference/api/sidePanel)
- [Chrome Debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger)
- [Chrome Alarms API](https://developer.chrome.com/docs/extensions/reference/api/alarms)
- [Extension service-worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)
- [Chrome extension cross-origin requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)
- [Google Docs keyboard shortcuts](https://support.google.com/docs/answer/179738)
