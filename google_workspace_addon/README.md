# LLM Wiki Google Docs add-on

This Google Workspace add-on is the in-Docs permission surface for the local
adapter. It asks for `drive.file` access to the active document and nothing
else. It never reads document text, sends content to a server, plans edits, or
writes the document.

The Apps Script project and the Desktop OAuth client used by the local adapter
must belong to the **same standard Google Cloud project**. That keeps the
per-file grant and the local `drive.file` token under one application identity.

## Create a test deployment

1. Create a standalone Apps Script project.
2. In **Project Settings**, change its Google Cloud project to the same project
   that contains the Desktop OAuth client and enrolled Docs API preview.
3. Copy `.clasp.json.example` to `.clasp.json` and set the Apps Script project
   ID, or copy `Code.gs` and `appsscript.json` in the Apps Script editor.
4. With Google's `clasp` CLI authenticated, run `clasp push` from this folder.
5. In Apps Script choose **Deploy > Test deployments > Google Workspace
   Add-on**, create the deployment, and install it for the enrolled account.
6. Open a disposable Google Doc, open **LLM Wiki** from the Workspace side
   panel, and click **Share this document**.

The tracked-suggestion API is still Developer Preview. Keep the deployment
private to the enrolled account or Workspace domain until Google permits a
public release.
