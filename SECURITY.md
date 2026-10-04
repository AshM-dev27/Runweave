# Security

Runweave is a development project. Use the current `main` revision for fixes; there are no supported stable releases or promised response times yet.

## Report a vulnerability

Use **Report a vulnerability** in this repository's [Security tab](https://github.com/AshM-dev27/Runweave/security). If private reporting is unavailable, open an issue requesting a private contact without disclosing the vulnerability. Do not post credentials, exploit details, task content or raw execution histories in public issues.

Include the affected commit, impact, a minimal reproduction and relevant redacted configuration. If a credential was exposed, revoke it at its provider; deleting a file does not remove it from Git history.

## Deployment boundaries

- The supplied Compose stack is for local development and binds exposed services to loopback. Runweave currently provides one authenticated workspace, not tenant isolation.
- Store `.env*`, database backups, artifacts and workflow histories privately. Histories can contain task inputs and tool outputs. Telemetry is disabled by default.
- The sandbox broker owns the Docker socket and is a trusted privileged component. Generated-code containers share the host kernel; they are not virtual machines.
- Install only trusted tool/MCP registrations and execution backends. An approval authorizes the displayed action; it cannot undo an external effect after it commits.
- Result schemas and evidence checks do not guarantee that a model's business decision is correct. Validate consequential outcomes in the integrating application.

See [operations](docs/operations.md) for credential handling, sandbox limits, upgrades and recovery.
