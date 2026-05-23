---
description: "GitHub workflow skills for managing repositories, pull requests, code reviews, issues, and CI/CD pipelines. When the `gh-app` wrapper is available on PATH it is the preferred entry point — zero credential input required. Otherwise use plain gh CLI / git with operator-provided auth."
---

## Quick auth pointer

Before writing any GitHub script:

- `command -v gh-app` → **yes** → use it for repos in its installation scope.
  See `github-auth/SKILL.md` Method 0. **Never ask the operator for `GITHUB_APP_ID`
  while `gh-app` is on PATH** — the operator already wired all credentials.
- Otherwise → use plain `gh` / `git` with whatever auth is configured.
