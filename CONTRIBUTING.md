# Contributing to JarvisMind

Thank you for helping. JarvisMind keeps people's memories, so changes are reviewed for correctness, privacy and
security before anything else.

## Before you start

- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
- For a security problem, do **not** open a public issue. Follow [SECURITY.md](SECURITY.md).
- Be kind. See the [code of conduct](CODE_OF_CONDUCT.md).

## Reporting bugs and asking for features

Use the issue templates. Remove memory text, tokens, client addresses, file paths that reveal a person, and any personal
details from logs and examples before you paste them. A reproduction with synthetic data is ideal.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Make the change with its tests. Governance, security and isolation changes need a test that refuses the wrong
   access, not only one that allows the right access.
3. Run the checks:
   - backend: `python -m pytest` from `backend/` (the whole suite; the release gate refuses partial runs);
   - console: `npm run typecheck`, `npm test`, `npm run build` from `console/`;
   - SDKs: `python -m pytest tests` in `sdk/python`, and `npm test` in `sdk/typescript`;
   - demos, when the change touches the API: the DSH demo against a running server.
4. Update the documentation that describes the behaviour you changed, and [CHANGELOG.md](CHANGELOG.md) for user-visible
   changes.
5. Open a pull request. Fill in the template, and explain what could go wrong and how you tested it.

## Rules that every change follows

- **One authority.** A memory is written only by JME, through the governed paths. Do not add a second copy of memory text.
- **Governance before writes.** A new write path must pass the credential gate and the sensitivity policy.
- **The server decides.** A permission check is in the server, never only in the console.
- **Agents cannot approve.** No agent route may return a confirmation token or approve a request.
- **No DDL in request handlers.** Schema changes come with a migration and an explicit upgrade script.
- **No secrets in the repository.** Tokens, keys, data directories, backups and logs stay out. The `.gitignore` covers them.
- **Synthetic data only.** Tests and examples use invented names and numbers.

## Style

- Python: type hints, Pydantic models with `extra="forbid"` for request bodies, specific exception types.
- TypeScript: strict mode. Display memory text as text, never as HTML.
- Comments say why. Remove comments that restate the code.
- Keep the public API in English. Console labels may be Chinese.

## Review

A maintainer reviews every pull request. Changes to governance, authentication, spaces, backups or exports get a second
review and a security note in the pull request. Keep pull requests focused, and avoid unrelated formatting changes.

## License

By contributing, you agree that your contribution is licensed under the MIT License of this project.
