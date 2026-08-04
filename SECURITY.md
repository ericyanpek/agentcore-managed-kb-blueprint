# Security and publication policy

## Local-only data

Do not commit:

- `config/test.env` or other local environment files
- `artifacts/`, including `state.env`, AWS API responses, and retrieval output
- downloaded source documents and extracted text generated under `artifacts/`
- virtual environments, caches, logs, or temporary renderings
- AWS credentials, account IDs, IAM identity ARNs, bucket names, or live
  knowledge base, data source, and ingestion job IDs

The checked-in `config/test.env.example` is the only environment template.
AWS credentials must come from the standard AWS CLI credential provider chain.

## Before publishing

Run:

```bash
./scripts/12_repository_safety_check.sh
git status --short --ignored
git diff --cached
```

The GitHub workflow runs the same safety check for pushes and pull requests.
The scanner is a release guard, not a replacement for GitHub secret scanning
or organizational credential rotation procedures.

If a credential is ever committed, revoke or rotate it first. Removing it from
the latest commit does not remove it from Git history.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed secret.
Use GitHub's private vulnerability reporting for this repository. Include the
affected script, reproduction steps, impact, and any suggested mitigation.
