# Security

## Reporting a problem

Email **itsme.deb1995@gmail.com** with what you found and how to reproduce it.
Please do not open a public issue for anything exploitable.

## What Argus is allowed to do

Argus investigates with read-only tools and proposes changes as Git pull
requests. It never writes to the cluster. The rules that enforce this are in
code ([`argus/guardrails.py`](argus/guardrails.py)), not in the model's prompt:

- only tools marked read-only (or explicitly allowlisted) can be seen or called
- hard limits on steps and tool calls
- secrets are redacted from tool output before the model sees it
- text that looks like instructions is flagged, and a flagged run cannot propose an automatic fix
- a proposed change must pass fixed limits (replicas, memory, protected namespaces, confidence) or it becomes an escalation
- every tool call is written to an audit log

The eval suite replays recorded incidents on every change, including one with
a prompt injection hidden in the logs.

## What is checked on every change

[`.github/workflows/security.yml`](.github/workflows/security.yml) runs on every
push to `main`, every pull request and once a week:

| Check | Tool | Fails the run when |
|---|---|---|
| Dependencies, including the demo app's | pip-audit | any known vulnerability |
| Code | Bandit | any medium or high finding |
| Secrets, full Git history | gitleaks | anything not listed in `.gitleaks.toml` |
| Dependencies, secrets, Kubernetes and Dockerfile config | Trivy | anything high or critical with a fix |
| Container image | Trivy | a critical issue with a fix; everything else is recorded in the Security tab |
| Static analysis | CodeQL, `security-extended` | results appear in the Security tab |

Suppressions are rare and each one says why: two `# nosec` comments in the
code and one entry in `.gitleaks.toml`.

## What this does not claim

Argus is an open-source tool, not a certified product. SOC 2 and HIPAA are
audits of an organisation and how it operates. The guardrails above are the
kind of controls those audits ask about (change approval, least privilege,
audit logging, data minimisation), and they make them easier to evidence.
