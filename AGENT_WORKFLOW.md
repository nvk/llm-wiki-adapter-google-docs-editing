# Google Docs editing agent workflow

This guide is the provider-specific handoff for the registered
`google-docs-editing` adapter. The public llm-wiki plugin should discover it
through `adapter route`; it must not duplicate these instructions.

## Boundaries

- This repository is a content-free tool. Keep document text, document IDs,
  OAuth material, edit specs, plans, hashes, journals, receipts, and results in
  the registered external data plane.
- A document URL alone does not authorize invented edits. Ask for a concrete
  instruction when it is missing or materially ambiguous.
- A bounded imperative approves only the smallest faithful plan. Any additional
  change needs a new instruction.
- Every successful write must be a tracked suggestion with the approved plan
  hash, expected revision, stable idempotency key, and independent verification.
- Never fall back to a direct Docs API mutation or arbitrary browser automation.

## Route handoff

1. Resolve the bundled `llm-wiki` CLI and keep the target URL private.
2. Run `adapter show google-docs-editing` and `adapter doctor
   google-docs-editing --json`. Do not continue through manifest drift or an
   unhealthy handshake.
3. Convert the target to its exact `google-docs:<document-id>` remote resource.
   Check that identifier against the registration before any read or write.
4. Registration and Google's per-file `drive.file` grant are separate. Run the
   content-free provider probe from the registered adapter root:

   ```bash
   .venv/bin/python adapter.py auth-status --document '<document-url>'
   ```

5. If the exact remote resource is not registered, use the pinned
   `auth --document '<document-url>'` flow, then re-register with `--replace`
   while preserving every existing read root, write root, environment name,
   and remote resource. If the resource is registered but the probe returns
   `picker_required: true`, run the same pinned authorization flow without
   asking for a second llm-wiki permission; the bounded edit already authorizes
   this repair. The user still completes Google's unavoidable provider UI.
   Preserve the refresh token and prior file grants, then rerun `auth-status`.
   Never blind-retry an unprobed 404.
6. For initial connector setup or diagnosis only, run `browser-status`. If the
   native host is not installed, run `browser-install` and follow its one-time
   unpacked-extension instructions. Normal edits require no extension click,
   port, pairing code, side-panel action, active-tab preparation, or manually
   opened dialog. Chrome must be running, signed in, and have the extension
   enabled.

## Governed edit

1. Run `inspect` privately and build the smallest exact-replacement edit spec.
2. Run `plan` against the required revision. Existing unresolved suggestions
   elsewhere are part of the baseline and do not block a non-overlapping plan.
   A target that overlaps suggested text fails only that plan. Pending or
   partial writes remain protected by duplicate and idempotency checks.
3. Hash the private plan and pass that same hash internally through
   `--approve-remote-write`; never ask the user to copy or repeat it.
4. Use a caller-stable idempotency key and the plan's expected revision. Write
   the complete response receipt only under a registered private output root.
5. Run `apply`. The native connector owns exact-document focus, Suggesting
   mode, Find-and-replace activation, and multi-edit execution. A failure before
   mutation is an adapter defect to diagnose or repair, not a reason to ask the
   user to prepare browser UI.
6. Run the separate `verify` operation against the verified receipt. UI
   completion alone is never success. Report only content-free status and
   counts unless the user explicitly requests document content.

See `README.md` for setup commands, request examples, supported edit shape, and
connector diagnostics.
