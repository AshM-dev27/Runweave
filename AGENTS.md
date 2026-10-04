# Project instructions

- Respect the user’s chosen model and reasoning level for Codex development. Any model the user selects is allowed; do not require a particular model or block work based on model choice.
- Use PLAN.md for current scope, README.md for setup, and docs/ for detailed usage, operations, and validation.
- Persist reports only for benchmarks. Share routine implementation, test, review, and cleanup results in chat; remove temporary diagnostic logs when finished.
- Keep public API schemas and events independent of PydanticAI and Temporal types.
- Keep workflow coordination deterministic; execute model and tool I/O through activities.
- Make run submission, persisted events, and retried tool effects idempotent or explicitly reconciled.
- Keep credentials out of events and traces; execute untrusted code only in isolated sandboxes.
- Use fake models for default tests. Cover API contracts and recovery paths when changing them.
- Keep roadmap, progress notes, and general coding advice out of this file.
