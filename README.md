# Google Docs Editing Adapter

Private, content-free llm-wiki tool for planning exact Google Docs edits,
applying them as tracked suggestions, and verifying those suggestions by reading
the document back in accepted and rejected projection modes.

- Repository: `nvk/llm-wiki-adapter-google-docs-editing`
- Manifest ID: `google-docs-editing`
- Protocol: `llm-wiki-adapter/v1`
- Version: `0.5.0`

The repository never stores document content, document identifiers, OAuth
credentials, tokens, plans, responses, journals, or receipts. All of those are
runtime material in operator-selected external directories.

## Tracked-changes guarantee

Every successful `apply`:

1. is bound to the SHA-256 of the exact plan file;
2. requires an explicit llm-wiki `--approve-remote-write` flag;
3. confirms the exact Docs API revision and both baseline projections before
   opening the editor;
4. opens an isolated, adapter-only Chrome profile, never the user's normal
   Chrome profile;
5. proves the normal Google Docs UI is in **Suggesting** mode before the first
   replacement and again after every replacement;
6. records a private pending journal immediately before the first UI mutation;
7. discovers the created suggestion IDs through Docs API read-back;
8. confirms those IDs exist in the inline projection;
9. confirms the rejected projection equals the pre-write document; and
10. confirms the accepted projection equals the approved replacement plan.

Any failed condition returns an error. A caller-stable idempotency key is
journaled outside the repository so retrying a successful operation cannot
create the suggestions twice.

The adapter no longer uses the Developer Preview suggestion-writing API. The
Docs API remains the revision-locked planning and verification channel; the
ordinary Google Docs browser UI is the write channel. This works with consumer
Google accounts that can use Suggesting in the document UI.

## Google prerequisite

Enable the Google Docs API, Google Drive API, and Google Picker API, then create
an OAuth **Desktop app** client. The API authorization requests only
`https://www.googleapis.com/auth/drive.file`. No Google Workspace subscription
or Developer Preview enrollment is required for browser-backed suggestions.

Google does not permit OAuth clients to be created or modified
programmatically. Creating the application identity in Google Cloud Console is
a one-time adapter-owner responsibility, not an end-user authorization step.
The downloaded client configuration is installed outside the repository. Users
then see only a **Connect with Google** button, Google consent, and Picker.

## Install and authenticate

Install the Python dependency into the private adapter environment. Playwright
uses the installed Google Chrome application; do not install or reuse a normal
Chrome user-data directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python adapter.py configure-oauth \
  --client-secrets /absolute/private/downloaded-desktop-client.json
.venv/bin/python adapter.py auth \
  --document 'https://docs.google.com/document/d/<document-id>/edit'
```

`configure-oauth` is the one-time owner provisioning command. It validates and
copies only the required Desktop client fields to the private mode-0600 profile
at `~/.config/llm-wiki/google-docs-editing/oauth-client.json`. The original
download remains external and can be removed after secure backup. Never commit
either file.

The normal `auth` command opens a one-click local page on a random `127.0.0.1`
port. It never asks the user for a credential file. **Connect with Google**
continues to Google's consent screen and Picker. The optional `--document`
value pins Picker to that exact document. The flow uses PKCE, a per-run OAuth
state, and only the `drive.file` scope; it filters to native Google Docs and
prints the selected exact `google-docs:<document-id>` resource.

The user token is stored separately at
`~/.config/llm-wiki/google-docs-editing/token.json` with mode `0600`; it does
not duplicate the managed client secret. Override the managed profile with
`GOOGLE_OAUTH_CLIENT_FILE` and the token with `GOOGLE_OAUTH_TOKEN_FILE` when
needed. Keep all credential material outside this repository.
Run `auth` again to grant the same OAuth client access to another document;
previously selected file IDs remain recorded in the private token metadata.

There is a second, one-time authorization for the browser write channel. Start
Codex with the tracked `codex-google-docs` dotfiles profile, then run:

```bash
.venv/bin/python adapter.py browser-auth \
  --document 'https://docs.google.com/document/d/<document-id>/edit'
```

Chrome opens with a dedicated user-data directory. Sign in to Google in that
window. The command succeeds only after the exact document editor and its mode
selector are ready. The dedicated profile defaults to `browser-profile/`
inside `LLM_WIKI_GOOGLE_DOCS_STATE_DIR`; it is runtime data and must never be
placed in this repository.

The default idempotency journal is
`~/.local/state/llm-wiki/google-docs-editing`. Optional path overrides belong in
the launcher environment, not in the repository or adapter registry:

```bash
export GOOGLE_OAUTH_TOKEN_FILE=/absolute/private/google-docs-token.json
export GOOGLE_OAUTH_CLIENT_FILE=/absolute/private/google-docs-oauth-client.json
export LLM_WIKI_GOOGLE_DOCS_STATE_DIR=/absolute/private/google-docs-state
export LLM_WIKI_GOOGLE_DOCS_BROWSER_PROFILE_DIR=/absolute/private/google-docs-browser
```

When using any override, add its name with `adapter add --env <NAME>` so the
sanitized llm-wiki launcher passes it through.

## Register one document

Use llm-wiki v0.20.0 or newer. Replace the placeholder with the exact document
ID from its Google Docs URL:

```bash
/path/to/llm-wiki adapter add "$PWD" \
  --read-root /absolute/private/google-docs-input \
  --read-root /absolute/private/google-docs-output \
  --write-root /absolute/private/google-docs-output \
  --remote-resource 'google-docs:<document-id>'
/path/to/llm-wiki adapter doctor google-docs-editing --json
```

Register additional documents explicitly by repeating `--remote-resource` and
then `adapter add --replace`. Normal `adapter list` output reports a count rather
than document IDs.

## Plan an exact replacement

Place this edit specification in the registered external read root:

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

`tab_id` can be omitted only for a single-tab document. Browser plans require
each `find` value to be unique across the entire document and do not accept `occurrence`.
v0.5 also refuses to plan while the document has any unresolved suggestions;
this keeps UI Find-and-replace deterministic and prevents interaction with
someone else's pending changes.

Run `inspect` first to obtain private tab text and IDs, then run `plan`. Both
operations are remote reads and produce only private artifacts in the external
output root. A plan records resolved document indices, accepted/rejected
projection hashes, the current revision, and its own SHA-256. It contains no
caller-supplied browser actions or mutating Docs API request body.

## Approve and apply

Build a mode-0600 apply request from the plan:

```bash
.venv/bin/python scripts/make_apply_request.py \
  --plan /absolute/private/google-docs-output/plan/plan.json \
  --output-dir /absolute/private/google-docs-output/apply-001 \
  --idempotency-key google-docs-apply-0001 \
  --request /absolute/private/google-docs-input/apply-001.json
```

The helper prints the plan SHA-256. Review the private plan and Google Docs diff,
then explicitly apply that exact hash:

```bash
/path/to/llm-wiki adapter run google-docs-editing \
  --request /absolute/private/google-docs-input/apply-001.json \
  --response /absolute/private/google-docs-output/apply-001-receipt.json \
  --approve-remote-write <exact-plan-sha256> \
  --json
```

Terminal JSON is content-free and identifier-free. The complete receipt stays
in the private output root with mode `0600`.

## Operations

- `self-test`: local UTF-16/indexing invariant check.
- `inspect`: private three-projection document inspection.
- `plan`: resolve exact replacements and generate a revision-locked plan.
- `apply`: create and verify Google Docs tracked suggestions.
- `verify`: check that receipt suggestion IDs and projections still match.

## Current limits

- v0.5 supports exact, unique body-text replacements, including multi-tab
  documents.
- The plan is refused when unresolved suggestions already exist.
- Browser writes are interactive/headful and require the dedicated profile to
  remain signed in.
- Header/footer settings, named-range creation, tab changes, and unsupported
  browser edit types are not generated.
- The tool never accepts arbitrary browser actions or raw `batchUpdate` JSON
  from the caller.
