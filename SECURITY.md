# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | Yes |

## Reporting a vulnerability

Please **do not** open a public issue for a security problem. Report it privately through the repository's security
advisory feature ("Report a vulnerability"), or to a maintainer directly if you cannot use that.

Include:

- the version, the deployment (source, Docker) and the operating system;
- the smallest set of steps that reproduces the problem;
- the impact you expect, and which credential or permission the attacker would need;
- a proposed fix, if you have one.

Use synthetic data in the reproduction. Do not include real memory text, real tokens, real addresses or personal details.
If you found a real token or real personal data during your work, stop, do not copy it further, and tell us that you found it.

## What to expect

We read every report and aim to acknowledge it within a reasonable time. We will confirm whether we can reproduce the issue,
work on a fix, and agree with you on disclosure. We credit reporters who want to be credited.

## Scope

In scope: the server, the console, the SDKs and the deployment files in this repository.

Out of scope: your own deployment choices (for example, exposing the server without a proxy), the security of the operating
system or the container runtime, and third-party models or services you connect.

The security model, the review results and the open items are described in [docs/SECURITY.md](docs/SECURITY.md) and
[docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md).
