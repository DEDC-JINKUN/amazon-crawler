# Amazon Collection agent entry

This repository is the recovered integration candidate for Amazon Collection. The active task is production-grade campaign control and bounded multi-Worker operation.

Before work involving campaigns, Worker count, Operator, deployment, full collection, production status, or release readiness, read [`docs/agent_handoff_current.md`](docs/agent_handoff_current.md) completely.

## Working rules

- Keep architecture decisions, integration, and final verification in the main agent.
- Treat code, deployed runtime, and documentation as separate facts. A local commit is not deployed.
- Use `collection_campaign` as the batch authority. Operator files are projections, not a second task source.
- Use tests first for fixes. New code requires tests; update CHANGELOG and the affected docs.
- Use PostgreSQL leases and the existing capacity gate. Preserve tenant and `subject_type` isolation.
- Keep `Agent/<name>` in configurable UAs. Do not implement TLS fingerprint spoofing, CAPTCHA bypass, login automation, or personal Cookie access.
- Work on code, documentation, and isolated tests unless the user explicitly authorizes deployment or live collection.
- Do not use production PostgreSQL or proxy credentials as test fixtures.
- Preserve unrelated files and the existing untracked runtime/test artifacts.

## Verification

- Run focused tests for the changed seam, then the broad suite with production credentials removed from the child environment.
- PostgreSQL concurrency acceptance must use the dedicated isolated test database; a skipped or unavailable engine test remains pending.
- Before a production claim, verify the deploy pointer and canonical `/readyz`; report requested, effective, and alive Workers separately.
