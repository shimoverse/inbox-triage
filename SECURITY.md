# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Report a vulnerability** button (Security → Advisories) rather than in a public issue. Include steps to reproduce with synthetic data. We aim to acknowledge reports within a week.

## Scope and design notes

- OAuth tokens, service-account keys, and API keys live only on the operator's machine (by default `~/.config/inbox-triage/`, mode 0600). This project never hosts credentials or mail.
- Email content is untrusted input. Models only answer fixed questions, and a local policy maps the answers to a small set of labels. Prompt injection can at most change which of those labels is applied, or cause none to be applied.
- Particularly interesting reports: anything that makes the tool send, delete, archive, or mark mail read; anything that leaks message content, addresses, or IDs beyond the documented provider payload; and anything that writes credentials or state with loose permissions.
