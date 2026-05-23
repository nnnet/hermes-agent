---
name: github-auth
description: "GitHub auth setup: gh-app wrapper (GitHub App installation token), HTTPS tokens, SSH keys, gh CLI login."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Authentication, Git, gh-cli, SSH, Setup, gh-app, GitHub-App]
    related_skills: [github-pr-workflow, github-code-review, github-issues, github-repo-management]
---

# GitHub Authentication Setup

This skill sets up authentication so the agent can work with GitHub repositories, PRs, issues, and CI. It covers three paths in priority order:

- **`gh-app` wrapper (if deployed)** — pre-configured GitHub App installation token. Operator side already wired all credentials, agent needs zero input from the user. **Try this first.**
- **`gh` CLI (if installed)** — richer GitHub API access with a simpler auth flow
- **`git` (always available)** — uses HTTPS personal access tokens or SSH keys

---

## Method 0: `gh-app` (PREFERRED when available — zero credential input)

**STOP. Before writing any GitHub script or asking the user for credentials — check
if `gh-app` exists on `PATH`. If yes, use it.**

```bash
command -v gh-app && gh-app --version 2>/dev/null  # detect
```

When deployed (e.g. inside the Hermes container) `gh-app` is a thin wrapper that:

1. Reads GitHub App credentials from env (`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`,
   `GITHUB_APP_PRIVATE_KEY_PATH` — operator-managed, **already populated**).
2. Mints a fresh installation token via JWT exchange (1-hour TTL, cached 50min).
3. Exports `GH_TOKEN` and exec's the regular `gh` CLI.

The wrapper requires **zero input from the user** — no App ID prompt, no PAT, no
`gh auth login`. If you ever find yourself asking the operator for `GITHUB_APP_ID`
or any other GitHub credential while `gh-app` is on `PATH`, you have made a
mistake — back up and use `gh-app`.

### Scope

`gh-app` is scoped to the App's installation org. Discover what's in scope:

```bash
gh-app api /installation/repositories --jq '.repositories[].full_name'
```

It **cannot** touch repos outside the installation (e.g. the operator's personal
repos). For those, fall back to plain `gh` (Method 1) — the operator has their
own auth on the host.

### Usage — common operations

```bash
# Inspect what the App can see
gh-app repo list <org> --limit 100
gh-app api /installation/repositories | jq '.total_count'

# Read / write a repo
gh-app repo view   <org>/<repo>
gh-app repo clone  <org>/<repo>          # clone via installation token (transparent)
gh-app issue list  --repo <org>/<repo>
gh-app pr create   --repo <org>/<repo> --title "..." --body "..."

# Delete (App needs Administration: write — same permission used to create)
gh-app repo delete <org>/<repo> --yes

# Direct API for endpoints `gh` doesn't expose
gh-app api -X DELETE /repos/<org>/<repo>
```

### Bulk operations pattern

Token is cached, so loops don't hammer the JWT endpoint:

```bash
for r in $(gh-app repo list <org> --limit 100 --json name --jq '.[].name' \
           | grep '^TEST-'); do
  echo "deleting <org>/$r"
  gh-app repo delete "<org>/$r" --yes
done
```

### Debugging when `gh-app` errors

- **403 on `gh-app api user`:** Expected — installation tokens can't impersonate
  a user. Use `/installation/repositories` instead.
- **404 on a repo you "know" exists:** Repo is outside the App's installation
  scope. Verify with `gh-app api /installation/repositories`.
- **`GITHUB_APP_ID not set`:** Operator-side wiring is broken. Check container env
  with `env | grep GITHUB_APP`. Do NOT ask the user to supply the ID — surface the
  env-wiring gap to the operator instead.

If `gh-app` errors and you genuinely need a different auth scope, **then** fall
back to Method 1/2 below — but state clearly to the operator what scope `gh-app`
lacks before asking for credentials.

---

## Detection Flow (when `gh-app` doesn't fit)

When a user asks you to work with GitHub on a repo OUTSIDE the App's installation
scope, run this:

```bash
# Check what's available
git --version
gh --version 2>/dev/null || echo "gh not installed"

# Check if already authenticated
gh auth status 2>/dev/null || echo "gh not authenticated"
git config --global credential.helper 2>/dev/null || echo "no git credential helper"
```

**Decision tree:**
1. If `gh-app` is on `PATH` AND target repo is in its installation scope → use Method 0
2. If `gh auth status` shows authenticated → use `gh` for everything
3. If `gh` is installed but not authenticated → use "gh auth" method below
4. If `gh` is not installed → use "git-only" method below (no sudo needed)

---

## Method 1: Git-Only Authentication (No gh, No sudo)

This works on any machine with `git` installed. No root access needed.

### Option A: HTTPS with Personal Access Token (Recommended)

This is the most portable method — works everywhere, no SSH config needed.

**Step 1: Create a personal access token**

Tell the user to go to: **https://github.com/settings/tokens**

- Click "Generate new token (classic)"
- Give it a name like "hermes-agent"
- Select scopes:
  - `repo` (full repository access — read, write, push, PRs)
  - `workflow` (trigger and manage GitHub Actions)
  - `read:org` (if working with organization repos)
- Set expiration (90 days is a good default)
- Copy the token — it won't be shown again

**Step 2: Configure git to store the token**

```bash
# Set up the credential helper to cache credentials
# "store" saves to ~/.git-credentials in plaintext (simple, persistent)
git config --global credential.helper store

# Now do a test operation that triggers auth — git will prompt for credentials
# Username: <their-github-username>
# Password: <paste the personal access token, NOT their GitHub password>
git ls-remote https://github.com/<their-username>/<any-repo>.git
```

After entering credentials once, they're saved and reused for all future operations.

**Alternative: cache helper (credentials expire from memory)**

```bash
# Cache in memory for 8 hours (28800 seconds) instead of saving to disk
git config --global credential.helper 'cache --timeout=28800'
```

**Alternative: set the token directly in the remote URL (per-repo)**

```bash
# Embed token in the remote URL (avoids credential prompts entirely)
git remote set-url origin https://<username>:<token>@github.com/<owner>/<repo>.git
```

**Step 3: Configure git identity**

```bash
# Required for commits — set name and email
git config --global user.name "Their Name"
git config --global user.email "their-email@example.com"
```

**Step 4: Verify**

```bash
# Test push access (this should work without any prompts now)
git ls-remote https://github.com/<their-username>/<any-repo>.git

# Verify identity
git config --global user.name
git config --global user.email
```

### Option B: SSH Key Authentication

Good for users who prefer SSH or already have keys set up.

**Step 1: Check for existing SSH keys**

```bash
ls -la ~/.ssh/id_*.pub 2>/dev/null || echo "No SSH keys found"
```

**Step 2: Generate a key if needed**

```bash
# Generate an ed25519 key (modern, secure, fast)
ssh-keygen -t ed25519 -C "their-email@example.com" -f ~/.ssh/id_ed25519 -N ""

# Display the public key for them to add to GitHub
cat ~/.ssh/id_ed25519.pub
```

Tell the user to add the public key at: **https://github.com/settings/keys**
- Click "New SSH key"
- Paste the public key content
- Give it a title like "hermes-agent-<machine-name>"

**Step 3: Test the connection**

```bash
ssh -T git@github.com
# Expected: "Hi <username>! You've successfully authenticated..."
```

**Step 4: Configure git to use SSH for GitHub**

```bash
# Rewrite HTTPS GitHub URLs to SSH automatically
git config --global url."git@github.com:".insteadOf "https://github.com/"
```

**Step 5: Configure git identity**

```bash
git config --global user.name "Their Name"
git config --global user.email "their-email@example.com"
```

---

## Method 2: gh CLI Authentication

If `gh` is installed, it handles both API access and git credentials in one step.

### Interactive Browser Login (Desktop)

```bash
gh auth login
# Select: GitHub.com
# Select: HTTPS
# Authenticate via browser
```

### Token-Based Login (Headless / SSH Servers)

```bash
echo "<THEIR_TOKEN>" | gh auth login --with-token

# Set up git credentials through gh
gh auth setup-git
```

### Verify

```bash
gh auth status
```

---

## Using the GitHub API Without gh

When `gh` is not available, you can still access the full GitHub API using `curl` with a personal access token. This is how the other GitHub skills implement their fallbacks.

### Setting the Token for API Calls

```bash
# Option 1: Export as env var (preferred — keeps it out of commands)
export GITHUB_TOKEN="<token>"

# Then use in curl calls:
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/user
```

### Extracting the Token from Git Credentials

If git credentials are already configured (via credential.helper store), the token can be extracted:

```bash
# Read from git credential store
grep "github.com" ~/.git-credentials 2>/dev/null | head -1 | sed 's|https://[^:]*:\([^@]*\)@.*|\1|'
```

### Helper: Detect Auth Method

Use this pattern at the start of any GitHub workflow:

```bash
# Try gh first, fall back to git + curl
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  echo "AUTH_METHOD=gh"
elif [ -n "$GITHUB_TOKEN" ]; then
  echo "AUTH_METHOD=curl"
elif [ -f ~/.hermes/.env ] && grep -q "^GITHUB_TOKEN=" ~/.hermes/.env; then
  export GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" ~/.hermes/.env | head -1 | cut -d= -f2 | tr -d '\n\r')
  echo "AUTH_METHOD=curl"
elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
  export GITHUB_TOKEN=$(grep "github.com" ~/.git-credentials | head -1 | sed 's|https://[^:]*:\([^@]*\)@.*|\1|')
  echo "AUTH_METHOD=curl"
else
  echo "AUTH_METHOD=none"
  echo "Need to set up authentication first"
fi
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `git push` asks for password | GitHub disabled password auth. Use a personal access token as the password, or switch to SSH |
| `remote: Permission to X denied` | Token may lack `repo` scope — regenerate with correct scopes |
| `fatal: Authentication failed` | Cached credentials may be stale — run `git credential reject` then re-authenticate |
| `ssh: connect to host github.com port 22: Connection refused` | Try SSH over HTTPS port: add `Host github.com` with `Port 443` and `Hostname ssh.github.com` to `~/.ssh/config` |
| Credentials not persisting | Check `git config --global credential.helper` — must be `store` or `cache` |
| Multiple GitHub accounts | Use SSH with different keys per host alias in `~/.ssh/config`, or per-repo credential URLs |
| `gh: command not found` + no sudo | Use git-only Method 1 above — no installation needed |
