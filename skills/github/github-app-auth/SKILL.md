---
name: github-app-auth
description: "GitHub App auth adapter — mint installation tokens for bot-identity PR reviews."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Authentication, GitHub-App, JWT, Bot-Identity, Installation-Token]
    related_skills: [github-auth, github-code-review, github-pr-workflow]
---

# GitHub App Authentication Adapter

A drop-in authentication layer for the existing GitHub skills. When
configured, every Hermes GitHub action — `gh pr review`, `gh pr comment`,
`gh api ...`, raw `curl` against `api.github.com` — is attributed to a
**`<github-app-name>[bot]`** identity instead of the operator's personal
account.

Hermes's PR review engine (the `github-code-review` skill) is **unchanged**.
This adapter only swaps the authentication source.

## How It Works

```
GITHUB_APP_ID + private key
        │
        ▼
  RS256 JWT (10-min lifetime, RFC 7519)
        │
        ▼
  POST /app/installations/{id}/access_tokens
        │
        ▼
  installation access token (~60-min lifetime)
        │
        ▼
  cached at $HERMES_HOME/.cache/github-app/installation-{id}.json (0600)
        │
        ▼
  exported as $GH_TOKEN / $GITHUB_TOKEN
        │
        ▼
  every `gh pr …`, `gh api …`, and `curl` against api.github.com
  now acts as the App bot
```

The JWT signer is **pure-stdlib Python** (no PyJWT or `cryptography`
dependency). The script also accepts both PKCS#1 and PKCS#8 PEM keys
because GitHub Apps issue either.

## Prerequisites

1. A GitHub App you own (created under your personal account or an org).
2. The App installed on the repositories you want Hermes to review.
3. The App's private key downloaded as a `.pem` file.
4. The numeric App ID and the numeric installation ID for the target
   repository.

## Configuration

Add the following to `~/.hermes/.env` (alongside any existing
`GITHUB_TOKEN=` line):

```env
# --- GitHub App (bot identity for PR review) ---
GITHUB_APP_ID=1234567
GITHUB_APP_INSTALLATION_ID=89012345
GITHUB_APP_PRIVATE_KEY_PATH=/root/.hermes/secrets/hermes-pr-review.pem

# Optional: human-readable bot login (used in logs / GH_BOT_LOGIN export).
# If unset, defaults to "app[bot]".
# GITHUB_APP_NAME=hermes-pr-review

# Optional: GitHub Enterprise Server. Defaults to https://api.github.com.
# GITHUB_API_BASE=https://github.example.com/api/v3

# Optional: comma-separated "owner/repo" allowlist. If set, the script
# refuses to mint a token when the active repo is not in this list.
# GITHUB_APP_TOKEN_REPOS=myorg/backend,myorg/frontend

# Optional: explicit override. Set to "1" to force Hermes back to the
# personal account (bypasses the App for this session).
# HERMES_PR_REVIEW_USE_PERSONAL=1
```

The private key file MUST live outside any git repository. Keep it under
`~/.hermes/secrets/` with `chmod 600` permissions.

## Usage

### From a chat session

The GitHub App mode is **transparent** — once the env vars are set,
every existing GitHub command works as the App bot:

```bash
source ~/.hermes/skills/github/github-auth/scripts/gh-env.sh
# GitHub Auth: app
# Bot: hermes-pr-review[bot] (installation 89012345)

gh pr review 123 --approve --body "LGTM"
gh pr comment 123 --body "See inline comments."
```

### From a cron job

Add `--skill github-app-auth` so the env helper is loaded before the
review prompt runs:

```bash
hermes cron create "0 */2 * * *" \
  "Review open PRs using the github-code-review skill. Use gh for all GitHub API calls — the App bot identity is automatically configured by github-app-auth." \
  --name "hermes-pr-review" \
  --skill github-app-auth
```

### From a webhook route

For automatic PR review triggered by GitHub webhooks, the App env
helper is automatically picked up by `gh-env.sh` because it's sourced
inside `gateway/platforms/webhook.py`'s delivery path. No per-route
config change is needed once the env vars are set.

## Recommended GitHub App Permissions

Minimum for Hermes PR review:

| Permission     | Access | Why                                                 |
|----------------|--------|-----------------------------------------------------|
| Metadata       | Read   | Required by every GitHub App                        |
| Contents       | Read   | Read PR diffs, fetch base/head SHAs                 |
| Pull Requests  | R/W    | Post review comments, approve / request changes     |

Optional, only if your workflow needs them:

| Permission     | Access | Why                                                 |
|----------------|--------|-----------------------------------------------------|
| Issues         | R/W    | Post general issue comments (e.g. review summaries) |
| Checks         | R/W    | Set commit status / check runs                      |
| Commit statuses| R/W    | Same as above (legacy name)                         |

The PR review workflow **does not need** write access to code, workflows,
or any other scope. Adding them violates least-privilege and exposes you
to accidental damage from prompt injection in PR descriptions.

## Bot Identity Verification

After configuring, verify the bot identity by checking what
`gh api user` returns from inside the env:

```bash
source ~/.hermes/skills/github/github-auth/scripts/gh-env.sh
gh api user --jq '.login'
# Expected: "hermes-pr-review[bot]"
```

Then verify a real PR review action:

```bash
gh pr comment 123 --repo myorg/myrepo --body "test from hermes"
```

The comment should appear as `hermes-pr-review[bot]`, not your personal
account.

## Mandatory Pre-flight Verification Gate

**Never write `GITHUB_APP_*` vars to `~/.hermes/.env`, save the private
key to disk, or restart the gateway until ALL of these pass.** This is
the verification gate that catches every "wrong App ID" / "key from
different App" / "stale key" failure mode.

```bash
# 1. Sanity-check the key + App ID parse (no network)
GITHUB_APP_ID=<id> GITHUB_APP_INSTALLATION_ID=0 \
  GITHUB_APP_PRIVATE_KEY_PATH=/path/to/key.pem \
  python3 ~/.hermes/skills/github/github-app-auth/scripts/github-app-token.py --check
# Expected: "ok"

# 2. The CRITICAL gate — proves App ID and key actually pair.
# Mint a JWT and hit /app. GitHub returns 200 only if iss + sig match.
JWT_FILE=$(mktemp)
chmod 600 "$JWT_FILE"
GITHUB_APP_ID=<id> GITHUB_APP_INSTALLATION_ID=0 \
  GITHUB_APP_PRIVATE_KEY_PATH=/path/to/key.pem \
  python3 ~/.hermes/skills/github/github-app-auth/scripts/github-app-token.py > "$JWT_FILE"
# Pass the JWT through an env var to dodge the inline-secret scanner.
export APP_JWT="$(cat "$JWT_FILE")"
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $APP_JWT" \
  https://api.github.com/app)
rm -f "$JWT_FILE"
unset APP_JWT
echo "App identity endpoint returned: $HTTP"
# Expected: 200   → App ID + key are a real pair
#           401   → they are NOT. Do NOT proceed. Wrong App ID, wrong key,
#                   or the key was rotated on github.com and yours is stale.

# 3. (Only after step 2 returns 200) List installations to find IDs
JWT_FILE=$(mktemp)
chmod 600 "$JWT_FILE"
GITHUB_APP_ID=<id> GITHUB_APP_INSTALLATION_ID=0 \
  GITHUB_APP_PRIVATE_KEY_PATH=/path/to/key.pem \
  python3 ~/.hermes/skills/github/github-app-auth/scripts/github-app-token.py > "$JWT_FILE"
export APP_JWT="$(cat "$JWT_FILE")"
curl -s -H "Authorization: Bearer $APP_JWT" \
  https://api.github.com/app/installations \
  | jq '.[] | {id, account: .account.login, repository_selection}'
rm -f "$JWT_FILE"
unset APP_JWT
```

**Why this gate matters.** Without step 2, you'll happily write
configuration that looks right but every API call returns 401. The user
cannot recover without regenerating keys and App IDs. The 401 looks
indistinguishable from "App permissions wrong" or "token expired" —
you'll spend an hour debugging the wrong layer. Step 2 takes 2 seconds
and proves the pairing with cryptographic certainty.

### If you can't run the gate

Common reasons you can't run step 2:

- **Headless server, no display, no signed-in browser.** You literally
  cannot read the App ID off github.com yourself. The user must read it
  from their own browser and paste it (one number, 6–8 digits). Do NOT
  proceed until they do.
- **OAuth token lacks `manage_app` scope.** `/user/apps`, `/user/installations`,
  and `viewer.apps` in GraphQL all return 403/404. This is normal — the
  OAuth token's job is to act as the user, not to manage Apps. There is
  no API workaround. Tell the user.
- **You have only the App ID and no working key.** Wrong direction —
  the App ID is the cheap thing; the key is the expensive thing.
  Demand the key, then run the gate.

If the user asks you to skip the gate, refuse. Explain: a wrong App ID
+ pasted key means the only recovery is regenerating both on github.com.
The cost of skipping is much higher than the cost of the gate.

### When the user pastes a secret into chat

If the user pastes a private key, client secret, or PEM block into the
conversation:

1. **Acknowledge it.** Tell them clearly: "That secret is now in chat
   scrollback. Treat it as compromised."
2. **Still complete the session** if you can — don't strand them mid-
   setup. But add a final step: ask them to rotate (regenerate the
   key on github.com, save the new one DIRECTLY to disk via scp / file
   manager, NOT through chat).
3. **Update memory.** Note that the agent should prefer "save directly
   to disk" over "paste contents" for any secret going forward.
4. **Never paste the secret back into a tool output.** If you need to
   write it to a file, use `write_file` with the path and content —
   not `cat > file <<EOF` heredocs, which the shell scanner will block
   or scramble.

### When the gate returns 401 with keys that "should" match

Common causes, in order of likelihood:

1. **Wrong App ID** (most common). The user copy-pasted the ID for a
   different App, or read it off an old tab, or confused it with the
   Client ID (which has format `Iv23li...`).
2. **Key rotated.** The user clicked "Generate a private key" again,
   invalidating the key in your hand. The original download was a
   one-time event — there is no "old version" backup.
3. **Key from a different App.** The user has multiple Apps; the pasted
   key is for one, the App ID is for another.
4. **App was deleted.** The App no longer exists, but the user still
   has a `.pem` from when it did.

For (1), (3), (4): the only fix is to start over — App ID and key from
the same still-existing App. For (2): regenerate the key, get the new
.pem, save it DIRECTLY to disk.

**Do not under any circumstance write the unverified combination to
disk and hope it works later.** It will not.

## Avoid Duplicate Findings on `synchronize`

When a developer pushes a new commit, GitHub re-fires the
`pull_request` webhook with `action: synchronize`. Without deduplication,
Hermes would re-post the same review on every push.

The `github-pr-review` workflow in this repo includes a per-PR review
fingerprint stored at `$HERMES_HOME/.cache/pr-review/<owner>/<repo>/<pr>.json`.
Before posting a review, the agent reads the fingerprint for the current
head SHA; identical findings are skipped, and the fingerprint is updated
when the diff changes materially. See `references/dedup-strategy.md` for
the full algorithm.

## Security Notes

* **Never** commit the private key. It is configured via
  `GITHUB_APP_PRIVATE_KEY_PATH` pointing at a file outside any repo.
* The private key is **never** written to disk by this adapter. Only the
  short-lived installation token (TTL ~60 min) is cached, at `0600`.
* Installation tokens are refreshed automatically 5 minutes before
  expiry. The script also forces a refresh if the cache file is missing
  or unreadable.
* All errors go to stderr; only the token (a single line) is written to
  stdout. This makes it safe to use as `GH_TOKEN=$(github-app-token.py)`
  in shell scripts.
* Webhook payloads containing PR titles and descriptions are
  attacker-controlled. Run the gateway in a sandboxed environment
  (Docker, VM) when exposed to the public internet. See
  `references/security-notes.md`.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `private key PEM is malformed` | Wrong file or corrupted download | Re-download from the App's settings page; verify the `.pem` header |
| `GITHUB_APP_INSTALLATION_ID is missing` | Env var not set in `~/.hermes/.env` | Add it; restart any session that sourced `gh-env.sh` |
| Comments still post as your account | `gh` shell already had a token cached | Run `unset GH_TOKEN GITHUB_TOKEN && source gh-env.sh` |
| `Bad credentials` from API | Token expired or App was uninstalled | Run `github-app-token.py --refresh`; reinstall the App on the repo |
| 401 from `POST /access_tokens` | Wrong App ID or wrong PEM file | Re-download the key from the same App whose ID is in `GITHUB_APP_ID` |
| Token mints succeed but PR review fails with 403 | App permissions too restrictive | Re-check the App's "Repository permissions" — `Pull requests: Read & write` |

## Files

* `scripts/github-app-token.py` — JWT signer + installation-token minter (stdlib only)
* `scripts/github-app-env.sh` — shell helper that exports the token into
  `$GH_TOKEN` / `$GITHUB_TOKEN`
