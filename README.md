# Google Docs Editing Adapter

Private, content-free llm-wiki tool for planning exact Google Docs edits,
applying them as tracked suggestions, and verifying those suggestions by reading
the document back in accepted and rejected projection modes.

- Repository: `nvk/llm-wiki-adapter-google-docs-editing`
- Manifest ID: `google-docs-editing`
- Protocol: `llm-wiki-adapter/v1`
- Version: `0.4.0`

The repository never stores document content, document identifiers, OAuth
credentials, tokens, plans, responses, journals, or receipts. All of those are
runtime material in operator-selected external directories.

## Tracked-changes guarantee

Every successful `apply`:

1. is bound to the SHA-256 of the exact plan file;
2. requires an explicit llm-wiki `--approve-remote-write` flag;
3. proves Developer Preview access with a read-only, comments-omitted
   `commentsViewMode` preflight before sending any edit;
4. sends `writeControl.writeMode: SUGGEST`;
5. sends the plan's `requiredRevisionId`;
6. requires Google to echo `writeMode: SUGGEST` and report
   `commentUpdateState: ALL_SAVED`;
7. captures the created suggestion IDs;
8. confirms those IDs exist in a `SUGGESTIONS_INLINE` read-back;
9. confirms the rejected projection equals the pre-write document; and
10. confirms the accepted projection equals the approved replacement plan.

Any failed condition returns an error. A caller-stable idempotency key is
journaled outside the repository so retrying a successful operation cannot
create the suggestions twice.

Google can silently ignore Developer Preview request fields for a project that
is not enrolled. The read-only preflight prevents that behavior from turning an
approved suggestion into a direct edit: if Preview access is not explicitly
confirmed, `apply` fails before the mutating `batchUpdate` request.

## Google prerequisite

Suggestion creation through the Docs API is currently Google Workspace
Developer Preview. The Google Workspace account and Cloud project must be
accepted into the Developer Preview Program. Enable the Google Docs API and
Google Drive API, enable the Google Picker API, and create an OAuth **Desktop
app** client for that project. The adapter requests only
`https://www.googleapis.com/auth/drive.file`.

This private adapter must not be shared as a public application while the
feature remains Pre-GA.

Google does not permit OAuth clients to be created or modified
programmatically. Creating the application identity in Google Cloud Console is
a one-time adapter-owner responsibility, not an end-user authorization step.
The downloaded client configuration is installed outside the repository. Users
then see only a **Connect with Google** button, Google consent, and Picker.

## Install and authenticate

The runtime has no third-party Python dependencies:

```bash
python3 -m venv .venv
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

The default idempotency journal is
`~/.local/state/llm-wiki/google-docs-editing`. Optional path overrides belong in
the launcher environment, not in the repository or adapter registry:

```bash
export GOOGLE_OAUTH_TOKEN_FILE=/absolute/private/google-docs-token.json
export GOOGLE_OAUTH_CLIENT_FILE=/absolute/private/google-docs-oauth-client.json
export LLM_WIKI_GOOGLE_DOCS_STATE_DIR=/absolute/private/google-docs-state
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

`tab_id` can be omitted only for a single-tab document. Add one-based
`occurrence` only when the find text legitimately appears more than once. A
plan fails if the target overlaps an unresolved existing suggestion.

Run `inspect` first to obtain private tab text and IDs, then run `plan`. Both
operations are remote reads and produce only private artifacts in the external
output root. A plan records exact Docs API requests, accepted/rejected
projection hashes, the current revision, and its own SHA-256.

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

- Developer Preview access is mandatory until Google makes suggestion writes GA.
- v0.4 supports exact body-text replacements, including multi-tab documents.
- Targets may not overlap unresolved suggestions; unrelated suggestions are
  preserved.
- Header/footer settings, named-range creation, tab changes, and unsupported
  suggest-mode request types are not generated.
- The tool never accepts arbitrary raw `batchUpdate` JSON from the caller.
