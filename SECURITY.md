# Security Policy

## Supported versions

Taimen is pre-1.0. Security fixes are made on the `main` branch of each
component and included in the next platform release; there are no long-term
support branches yet.

| Platform version | Supported |
|---|---|
| latest `main` / latest `v0.x` tag | yes |
| older tags | no |

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Report privately through GitHub's private vulnerability reporting: open the
**Security** tab of the affected repository and choose **Report a vulnerability**.
If that option is not available to you, open an issue that only says
"security: please contact me" without any details, and a maintainer will reach
out through a private channel. Include in the private report:

- the component and version (tag or commit),
- steps to reproduce or a proof of concept,
- the impact you expect (what an attacker gains),
- whether the issue is already public.

You will get an acknowledgement within 5 business days and a status update at
least every 14 days until the issue is resolved. We ask for coordinated
disclosure: please give us up to 90 days to ship a fix before publishing details.
Credit is given in the release notes unless you prefer to stay anonymous.

## Scope

In scope: the code in this repository — the Taimen Skill SDK (`src/skill_sdk`: the skill contract, the invocation context, the personal-data guard of `ctx.llm`, the hosts).

Out of scope: third-party dependencies (report upstream; tell us if a fix
requires a coordinated update), skills written by third parties with the SDK, and the LLM providers, demo data, and deployments run by
third parties.

## Hardening notes for operators

- Pass secrets to skills through the host's secret store (`ctx.secret`), never through skill parameters.
- Keep the personal-data guard of `ctx.llm` enabled for prompts built from business data.
