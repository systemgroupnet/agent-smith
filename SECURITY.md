# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(**Security → Report a vulnerability** on this repository). Include what you found, how to
reproduce it, and what an attacker could do with it.

We aim to acknowledge a report within 5 working days and to agree a disclosure timeline with you.

## Scope

Especially interesting:

- A reviewer agent writing to the checkout, executing commands, or reading outside it.
- Credentials (GitLab token, LLM keys, webhook secrets) leaking into logs, comments,
  analytics events or traces.
- Content in a merge request (prompt injection) causing actions beyond posting review comments.
- Collector endpoints accepting forged events or webhooks when a secret is configured.

## Deployment notes

- `GITLAB_TOKEN` needs `api` scope in order to post comments. Use a dedicated bot account.
- Set `GITLAB_WEBHOOK_SECRET` and `COLLECTOR_TOKEN` on any collector reachable from outside
  your network.
- Reviewed code is sent to your configured LLM provider. Choose one whose data policy
  fits your code.
