# Private llm-wiki Adapter Instructions

This repository is a tool, never a content store.

- Keep the repository private.
- Commit only executable code, manifests, documentation, tests, and synthetic
  fixtures generated inside temporary test directories.
- Never commit real source content, document IDs, edit plans, OAuth credentials,
  tokens, API responses, receipts, documents, corpora, or generated results.
- Inputs, credentials, journals, and outputs belong to explicitly registered
  external roots.
- The adapter must declare `writes_wiki: false` and must never write to a wiki.
- Every remote write must use Google Docs suggest mode, an approved plan hash,
  a required revision ID, an idempotency key, and read-back verification.
- Run all tests and inspect `git ls-files` before every push.
