# Shared Browser Executor Extraction Spike

Date: 2026-08-17

## Result

The reuse and typed-action gates pass for a foundation build. The synthetic
Google Docs mutation program and the synthetic read-only X Space program share
seven operations:

- `open_or_focus_exact_url`
- `assert_exact_target`
- `attach_debugger`
- `wait_ax`
- `first_success`
- `click_ax`
- `detach_debugger`

Neither program contains arbitrary JavaScript, `Runtime.evaluate`, downloaded
code, or natural-language instructions. This is enough reuse to build the
provider-neutral transport and read-only typed interpreter. It is not evidence
that the complete Google Docs workflow can migrate yet; that remains gated on a
no-eval parity test.

## Current worker classification

The machine-checked extraction ledger accounts for all 71 named functions in
`extension/service-worker.js`:

| Category | Count | Decision |
|---|---:|---|
| Transport/lifecycle | 14 | Candidate for the shared connector after generic errors and state names |
| Generic CDP | 16 | Replace with fixed typed actions or executor internals |
| Provider targeting | 6 | Keep in the Google Docs action compiler |
| Provider driver | 34 | Keep in the Google Docs adapter; compile bounded actions |
| Forbidden generic | 1 | Remove `evaluate`; never expose it through the shared executor |

The shared layer must not be created by moving `service-worker.js` wholesale.
Only transport/lifecycle mechanics and typed CDP primitives qualify. The
Google-specific target parser, mode handling, Find-and-replace logic, recovery
ordering, and mutation meaning remain provider-owned.

## Next development slice

Create the private `llm-wiki-adapter-browser-execution` foundation with:

1. a versioned job validator;
2. canonical hashing and exact-target constraints;
3. a private Unix-socket client and Native Messaging relay;
4. a route-free content-free manifest;
5. a shared extension shell with no provider logic; and
6. negative tests that reject evaluation, mutation in read jobs, wrong origins,
   unbounded actions, and content-bearing public results.

Do not change the production Google Docs connector or its installed extension
during this slice.
