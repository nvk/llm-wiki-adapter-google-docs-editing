# Google setup and OAuth

This setup uses one **Desktop app** OAuth client and Google's official Desktop
Picker authorization flow. The user selects one exact Google Doc, granting that
same client `drive.file` access to that file. The adapter then calls the Docs API
directly.

Use the same Google account throughout. Do not create an API key, service
account, web client, Apps Script project, Workspace add-on, or Chrome Extension
client.

## 1. Select the Google Cloud project

Use the project that owns the Desktop OAuth client, or create one at:

<https://console.cloud.google.com/projectcreate>

Keep it selected for every step below.

## 2. Enable the APIs

Enable both APIs in that project:

1. [Google Docs API](https://console.cloud.google.com/apis/library/docs.googleapis.com)
2. [Google Picker API](https://console.cloud.google.com/apis/library/picker.googleapis.com)

The Picker flow does not require an API key. The `drive.file` value below is an
OAuth scope; the adapter does not need the Drive API for its Docs operations.

## 3. Configure Google OAuth consent

1. Open [Google Auth Platform → Branding](https://console.cloud.google.com/auth/branding).
2. If prompted, click **Get Started** and enter the app name, support email,
   audience, and contact email.
3. For an **External** app in Testing, open
   [Audience](https://console.cloud.google.com/auth/audience) and add the exact
   Google account used in Docs as a test user.
4. Open [Data Access](https://console.cloud.google.com/auth/scopes).
5. Keep only this required scope for this app:

   ```text
   https://www.googleapis.com/auth/drive.file
   ```

6. Remove `script.external_request` if it was added for the retired Apps Script
   bridge. Do not add broad `drive`, `drive.readonly`, or `documents` scopes.

Desktop Picker permits only `drive.file` and cannot combine it with another
scope. It is a non-sensitive, per-file scope. An External app left in Testing
can require reauthorization after seven days.

## 4. Create and download the Desktop OAuth client

1. Open [Google Auth Platform → Clients](https://console.cloud.google.com/auth/clients).
2. Click **Create Client**.
3. Choose **Desktop app**.
4. Name it and click **Create**.
5. Download its JSON file.

Treat the download as a credential. Do not commit it, paste it into chat, or
copy it into this repository.

## 5. Import the client

From the adapter checkout, locate the file without relying on an unmatched shell
wildcard:

```bash
find "$HOME/Downloads" -maxdepth 1 -type f -name 'client_secret_*.json' -print
```

Pass the exact quoted path printed by `find`:

```bash
./scripts/google_docs_auth.py configure \
  "$HOME/Downloads/client_secret_ACTUAL_NAME.apps.googleusercontent.com.json"
```

You may instead type `./scripts/google_docs_auth.py configure `, drag the JSON
from Finder into Terminal, and press Return.

## 6. Authorize one exact Google Doc

During normal use, the agent starts this command itself and gives you one short
local link. Open that link, select the displayed document, and click **Insert**.
You should not have to copy the adapter path or Docs URL into Terminal.

For a manual test from the adapter checkout, run:

```bash
./scripts/google_docs_auth.py authorize \
  'https://docs.google.com/document/d/DOCUMENT_ID/edit'
```

The browser opens Google consent and then Google Picker, filtered to that exact
Google Doc. Select it and click **Insert**. The local callback verifies that
Picker returned the requested document before storing the token.

If the browser cannot be opened automatically, use `--no-browser`. It prints a
short loopback link such as `http://127.0.0.1:43210/start`; opening it redirects
locally to Google. The long provider OAuth URL is never printed, so it cannot be
corrupted by terminal wrapping or TUI padding.

Check content-free status:

```bash
./scripts/google_docs_auth.py status --json
```

A successful result has this shape:

```json
{
  "configured": true,
  "connected": true,
  "file_selection": "google-picker",
  "scopes": ["https://www.googleapis.com/auth/drive.file"],
  "status": "ok",
  "token_source": "stored"
}
```

The token is private under
`~/.config/llm-wiki/google-docs-editing/oauth/`. Status never prints client
secrets, access tokens, refresh tokens, or selected document IDs.

When another file needs access, give its normal Docs URL to the agent. The agent
starts the helper and presents the short local link. Because Google issues a new
Picker authorization token, completing that flow replaces the locally stored
token used by the adapter.

## 7. Install and verify the adapter

```bash
./scripts/install_local.py
./scripts/google_docs_auth.py status --json
python3 -m unittest discover -s tests -v
```

Use a disposable synthetic document for the first live test. A normal editing
session is:

1. give the agent the exact Docs URL and complete its short local Picker link;
2. ask the agent to plan suggestions, an anchored comment, an assigned comment,
   or an in-document person mention for that URL;
3. inspect and explicitly approve the concrete plan; and
4. let the adapter apply and verify native suggestions and/or comment threads.

## Troubleshooting

### `zsh: no matches found: ...client_secret_*.json`

No matching file existed, so zsh did not run the adapter. Locate the download
with `find` and pass the exact quoted path.

### Picker does not open or reports an invalid request

Confirm the Google Picker API is enabled in the same project as the Desktop
OAuth client. Confirm the authorization request uses only `drive.file`; remove
`script.external_request` from Data Access and re-run `authorize`.

### `access_denied` or “app is being tested”

For an External app, add the signing-in account under **Google Auth Platform →
Audience → Test users**. Also confirm the browser chose that account.

### Authorization expires after a week

That can happen for an External app in Testing. Re-run `authorize '<URL>'`, or
use an Internal/production configuration appropriate for the account.

### `403` or insufficient permissions for a document

Re-run `authorize` with that exact Docs URL and select the displayed file in
Picker. Confirm the Docs API and Picker API are enabled in the Desktop client's
Cloud project and that the browser used the same Google account that can edit
the document.

### Migrating from version 0.13.0

Version 0.13.0 used a Workspace add-on and Apps Script API executable, but that
execution identity does not inherit the add-on's per-file grant. Version 0.14.0
replaces that bridge with the provider-supported Desktop Picker flow.

After 0.14.0 authorization works, you may uninstall the old **LLM Wiki** test
add-on, archive/delete its Apps Script project and API-executable deployment,
disable the Apps Script API if nothing else uses it, and remove
`script.external_request` from OAuth Data Access.

## Official references

- [Integrate Picker into desktop and mobile apps](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)
- [Choose Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Configure OAuth consent](https://developers.google.com/workspace/guides/configure-oauth-consent)
- [Create a Desktop OAuth client](https://developers.google.com/workspace/guides/create-credentials)
- [Docs API release notes](https://developers.google.com/workspace/docs/release-notes)
