# LLM Wiki Google Docs add-on

This Google Workspace add-on is the in-Docs permission surface and private
Google-only relay for the local adapter. It asks for `drive.file` access to the
active document and `script.external_request` to call only the allowlisted Docs
API endpoint. It exposes two fixed API-executable functions for reading that
file and submitting a strictly validated native-suggestion batch. Planning and
approval remain local, and the script's OAuth token never leaves Google.

The Apps Script project and the Desktop OAuth client used by the local adapter
must belong to the **same standard Google Cloud project**. That lets the
Desktop client call the exact private script through `scripts.run`, while the
add-on's own identity retains the per-file grant.

## Create a test deployment

The full prerequisite and OAuth sequence is in [`../SETUP.md`](../SETUP.md).
For the add-on itself:

1. At <https://script.google.com/home>, create a standalone Apps Script project.
2. In **Project Settings**, enable **Show `appsscript.json` manifest file in
   editor**.
3. Under **Google Cloud Project**, click **Change project** and enter the
   numeric project number of the same standard Cloud project that contains the
   Desktop OAuth client. Click **Set project**.
4. In **Editor**, replace `Code.gs` and `appsscript.json` with the files in this
   directory, then save.
5. Choose **Deploy > Test deployments**, click **Install**, then **Done**.
6. Open or refresh a disposable Google Doc, open **LLM Wiki** from the
   Workspace side panel, authorize it if prompted, and click **Share this
   document**.
7. Choose **Deploy > New deployment > API Executable**, restrict access to
   **Only myself**, deploy it, and copy its Deployment ID.
8. Run `../scripts/google_docs_auth.py bridge 'DEPLOYMENT_ID'`, then repeat
   local OAuth login so the caller token has both manifest scopes.

Alternatively, copy `.clasp.json.example` to `.clasp.json`, set its Apps Script
project ID, authenticate Google's `clasp` CLI, and run `clasp push` from this
directory before installing the test deployment.

Google made the tracked-suggestion API generally available on September 30,
2026. Developer Preview enrollment is not required. An unpublished test
deployment remains the simplest option for personal use.
