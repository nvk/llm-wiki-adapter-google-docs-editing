# Google setup and OAuth

This setup uses one Google Cloud project for two clients:

- a **Desktop app** OAuth client used by the local adapter; and
- a private Google Workspace add-on that lets you grant `drive.file` access to
  one open document at a time.

Use the same Google account throughout. Do not create an API key, service
account, web client, or Chrome Extension client.

Google made Docs API comments and suggestions generally available on
September 30, 2026. Developer Preview enrollment is no longer required.

## 1. Create or select a Google Cloud project

1. Open <https://console.cloud.google.com/projectcreate>.
2. Create a project, for example **LLM Wiki Google Docs**.
3. Keep that project selected for every Google Cloud step below.
4. Record both its **Project ID** and numeric **Project number**. The Apps
   Script step uses the project number, not the ID.

## 2. Enable the Google Docs API

1. Open <https://console.cloud.google.com/apis/library/docs.googleapis.com>.
2. Confirm the project selector shows the project from step 1.
3. Click **Enable**.

The add-on code does not use the Drive API. The `drive.file` text below is an
OAuth scope, not a requirement to enable the Drive API.

## 3. Configure Google OAuth consent

1. Open **Google Auth platform > Branding**:
   <https://console.cloud.google.com/auth/branding>.
2. If Google says the Auth platform is not configured, click **Get Started**.
3. Enter an app name such as **LLM Wiki Google Docs** and select your email as
   the user-support email.
4. For **Audience**:
   - choose **Internal** if this is a Google Workspace project and only people
     in that organization will use it; otherwise
   - choose **External**.
5. Enter your email under **Contact Information**, accept the User Data Policy,
   and create the configuration.
6. If you chose **External**, open **Audience > Test users**, click **Add
   users**, and add the exact Google account you will use in Docs.
7. Open **Data Access > Add or Remove Scopes** and add only:

   ```text
   https://www.googleapis.com/auth/drive.file
   ```

   Save the scope selection. Do not add the broad `drive`, `drive.readonly`, or
   `documents` scopes.

`drive.file` is Google's non-sensitive, per-file scope. An External app left in
**Testing** works for personal setup, but Google expires its authorization and
refresh token after seven days. You can sign in again, use an Internal app, or
move an External app to production when appropriate.

## 4. Create and download the Desktop OAuth client

1. Open **Google Auth platform > Clients**:
   <https://console.cloud.google.com/auth/clients>.
2. Click **Create Client**.
3. Select **Desktop app** as the application type.
4. Name it, for example **LLM Wiki local adapter**, and click **Create**.
5. In the OAuth client list, download that client's JSON file. Leave the file
   in `~/Downloads` for the next step.

The downloaded file normally starts with `client_secret_`. Treat it as a
credential: do not commit it, paste it into chat, or copy it into this repo.

## 5. Import the client and sign in locally

From the adapter worktree, first locate the downloaded JSON without relying on
a shell wildcard:

```bash
find "$HOME/Downloads" -maxdepth 1 -type f -name 'client_secret_*.json' -print
```

Copy the exact path printed by that command. Then run these commands, replacing
the example filename with the real one:

```bash
./scripts/google_docs_auth.py configure \
  "$HOME/Downloads/client_secret_ACTUAL_NAME.apps.googleusercontent.com.json"
./scripts/google_docs_auth.py login
./scripts/google_docs_auth.py status --json
```

You can also type `./scripts/google_docs_auth.py configure ` (including the
trailing space), drag the JSON file from Finder into Terminal, and press
Return. Finder supplies the exact path.

During login, choose the same account you added as a test user. If Google shows
a testing or unverified-app warning, continue only after confirming that the
displayed app and Cloud project are yours. A successful status looks like:

```json
{
  "configured": true,
  "connected": true,
  "scope": "https://www.googleapis.com/auth/drive.file",
  "status": "ok",
  "token_source": "stored"
}
```

The importer copies only the required client settings to the private OAuth
directory. The downloaded JSON can then be removed from `~/Downloads`. Stored
files live under `~/.config/llm-wiki/google-docs-editing/oauth/` with private
permissions. Never send those files or their contents to an agent or chat.

## 6. Install the per-document Google Docs add-on

The add-on is what makes a document eligible for the Desktop client's
`drive.file` token.

1. Open <https://script.google.com/home> and click **New project**.
2. Name the standalone Apps Script project **LLM Wiki**.
3. Open **Project Settings** and enable **Show `appsscript.json` manifest file
   in editor**.
4. Under **Google Cloud Project**, click **Change project**. Enter the numeric
   **Project number** from step 1 and click **Set project**.
5. Return to **Editor**. Replace the contents of `Code.gs` with the contents of
   [`google_workspace_addon/Code.gs`](google_workspace_addon/Code.gs).
6. Replace the contents of `appsscript.json` with the contents of
   [`google_workspace_addon/appsscript.json`](google_workspace_addon/appsscript.json).
7. Save the project.
8. Choose **Deploy > Test deployments**, click **Install**, and then **Done**.
9. Open or refresh a disposable Google Doc. In the right-side Workspace panel,
   open **LLM Wiki** and authorize the add-on if prompted.
10. Click **Share this document**. The card should change to **Ready**.

An unpublished test deployment is appropriate for personal use. Other testers
need editor access to the Apps Script project and must belong to the same
domain as its owner.

## 7. Verify before the first edit

Run the local checks:

```bash
./scripts/google_docs_auth.py status --json
python3 -m unittest discover -s tests -v
```

Then use a disposable document for the first live suggestion. The normal user
flow is:

1. open the document;
2. open **LLM Wiki** in the Docs side panel and click **Share this document**;
3. ask the agent to edit that exact Docs URL using suggestions; and
4. approve the concrete edit plan before the adapter writes anything.

## Troubleshooting

### `zsh: no matches found: ...client_secret_*.json`

No matching file exists. The adapter command did not run. Finish step 4,
locate the download with `find`, and pass its exact quoted path instead of a
wildcard.

### `access_denied` or “app is being tested”

For an External app, add the signing-in account under **Google Auth platform >
Audience > Test users**. Make sure the browser did not select a different
Google account.

### Login works and then expires a week later

That is expected for an External app in **Testing**. Run
`./scripts/google_docs_auth.py login` again, or use an Internal/production
configuration appropriate for your account.

### `403` or “insufficient permissions” for a document

Confirm all of the following:

- the Docs API is enabled in the same Cloud project as the Desktop client;
- the Apps Script project is linked to that same numeric project number;
- the same Google account installed the add-on and completed Desktop login; and
- **Share this document** was clicked in that specific document.

### The add-on does not appear in Docs

Return to **Deploy > Test deployments** and confirm it is installed, then
refresh the Docs tab. Test deployments are installed per account.

## Official references

- [Configure OAuth consent](https://developers.google.com/workspace/guides/configure-oauth-consent)
- [Create a Desktop OAuth client](https://developers.google.com/workspace/guides/create-credentials)
- [Choose Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Link Apps Script to a standard Cloud project](https://developers.google.com/apps-script/guides/cloud-platform-projects)
- [Install an unpublished Workspace add-on](https://developers.google.com/workspace/add-ons/how-tos/testing-workspace-addons)
- [Docs API release notes](https://developers.google.com/workspace/docs/release-notes)
