## What this changes

<!-- One or two sentences. -->

## Why

## How it was tested

- [ ] `python -m pytest` in `backend/` (full suite)
- [ ] `npm run typecheck`, `npm test`, `npm run build` in `console/` (if the console changed)
- [ ] `npm test` in `sdk/typescript` and `python -m pytest tests` in `sdk/python` (if the SDKs changed)
- [ ] The DSH demo against a running server (if the API changed)

## Checklist

- [ ] Documentation updated (`docs/`, and `CHANGELOG.md` for user-visible changes)
- [ ] Access control changes have a test that refuses the wrong access
- [ ] No memory is written by a path that skips the credential gate or the sensitivity policy
- [ ] No agent route returns a confirmation token or approves a request
- [ ] No secrets, data directories, backups or logs are included in this change

## Security notes

<!-- Anything a reviewer should look at closely. -->
