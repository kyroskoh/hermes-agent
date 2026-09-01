# GitHub App Quickstart for Hermes PR Review

Complete step-by-step setup. Total time: ~5 minutes.

## Step 1: Create the GitHub App

1. Go to **GitHub → Settings → Developer settings → GitHub Apps**.
   - URL: `https://github.com/settings/apps/new` (personal)
   - URL: `https://github.com/organizations/<org>/settings/apps/new` (org)
2. Fill in the form:
   - **GitHub App name**: `hermes-pr-review` (must be globally unique;
     append your org name if needed)
   - **Homepage URL**: `https://github.com/<your-username>`
   - **Identifying and authorizing users**: skip
   - **Post installation**: leave the default "Off"
   - **Webhook**: leave **Active** *unchecked* — Hermes uses its own
     webhook receiver, not GitHub's App webhook delivery.
3. **Repository permissions** (this is the critical section):
   - Metadata: **Read-only** (required by every App)
   - Contents: **Read-only** (read diffs and PR branches)
   - Pull requests: **Read and write** (post review comments)
   - Issues: leave "No access" unless your workflow posts issue comments
   - **All other permissions: "No access"** — least privilege
4. Click **Create GitHub App**.

## Step 2: Generate and Save the Private Key

1. On the App's settings page, scroll to **Private keys**.
2. Click **Generate a private key**.
3. GitHub downloads a `.pem` file (typically `hermes-pr-review.<timestamp>.pem`).
4. Save it to **a directory outside any git repository**:
   ```bash
   mkdir -p ~/.hermes/secrets
   mv ~/Downloads/hermes-pr-review.*.pem ~/.hermes/secrets/hermes-pr-review.pem
   chmod 600 ~/.hermes/secrets/hermes-pr-review.pem
   ls -la ~/.hermes/secrets/hermes-pr-review.pem
   # -rw------- 1 you you 1704 ... hermes-pr-review.pem
   ```

> **Never** commit this file to a repository. Add `*.pem` to every
> project's `.gitignore` as a belt-and-braces measure.

## Step 3: Note the App ID

On the App's settings page, look at **About → App ID** (top right). It's
a numeric value like `1234567`. Copy this.

> Also note the **Client ID** — you don't need it for App auth (it's
> only for OAuth-style flows).

## Step 4: Install the App on Your Repositories

1. On the left sidebar, click **Install App**.
2. Click **Install** next to your account or org.
3. Choose **Install on selected repositories** (not "All repositories"
   — least privilege).
4. Select the repos you want Hermes to review.
5. Click **Install**.

## Step 5: Find the Installation ID

The installation ID is a per-installation numeric value. To find it:

```bash
# Use your personal PAT temporarily (or any token with admin:org scope
# if it's an org App).
curl -s -H "Authorization: token $YOUR_PERSONAL_PAT" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/app/installations \
  | python3 -c "
import sys, json
for i in json.load(sys.stdin):
    print(f\"  id={i['id']}  account={i['account']['login']}  repos={i.get('repository_selection')}\")
"
```

You'll see something like:

```
  id=89012345  account=yourorg  repos=selected
```

Copy the `id` value — that's `GITHUB_APP_INSTALLATION_ID`.

## Step 6: Configure Hermes

Add to `~/.hermes/.env`:

```env
# --- GitHub App (bot identity for PR review) ---
GITHUB_APP_ID=1234567
GITHUB_APP_INSTALLATION_ID=89012345
GITHUB_APP_PRIVATE_KEY_PATH=/root/.hermes/secrets/hermes-pr-review.pem
GITHUB_APP_NAME=hermes-pr-review
```

Reload Hermes (or start a new chat session) so the env is picked up.

## Step 7: Verify the Setup

In a Hermes chat session:

```text
Verify the GitHub App auth is working:

1. source ~/.hermes/skills/github/github-auth/scripts/gh-env.sh
2. gh api user --jq '.login'
3. Run gh api user | head -20
```

You should see:

```
GitHub Auth: app
Bot: hermes-pr-review[bot] (installation 89012345)
hermes-pr-review[bot]
{"login":"hermes-pr-review[bot]","id":...,"type":"Bot",...}
```

> Note the `"type": "Bot"` field — that's how GitHub classifies App
> identities.

## Step 8: Test on a Real PR

Open a test PR on one of the repos where the App is installed, then ask
Hermes:

```text
Review PR #1 in myorg/myrepo using the github-code-review skill.
Post the review as a comment.
```

The comment should appear as `hermes-pr-review[bot]`, not your personal
account.

## Setting Up Automatic Review (Webhook)

For real-time reviews triggered by PR events, see
`website/docs/guides/webhook-github-pr-review.md` (or the local
`/usr/local/lib/hermes-agent/website/docs/guides/webhook-github-pr-review.md`).
The GitHub App env helper is automatically picked up — no per-route
config change is needed once the env vars are set.

## Setting Up Scheduled Review (Cron)

```bash
hermes cron create "*/30 * * * *" \
  "Review open PRs in myorg/myrepo.

  Steps:
  1. source ~/.hermes/skills/github/github-auth/scripts/gh-env.sh
  2. Run: gh pr list --repo myorg/myrepo --state open --json number,title,headRefOid
  3. For each PR updated in the last hour:
     - Run: gh pr diff NUMBER --repo myorg/myrepo
     - Review using the github-code-review skill
     - gh pr review NUMBER --repo myorg/myrepo --comment --body 'YOUR_REVIEW'

  Skip PRs whose findings are identical to the previous review — use
  pr-review-dedup.py to check.

  If no new PRs, say 'No new PRs to review.' and exit." \
  --name "hermes-pr-review" \
  --skill github-app-auth
```

## Revoking Access

To remove Hermes's bot access:

1. **Per-repository**: GitHub → Repo → Settings → Integrations →
   hermes-pr-review → Configure → Uninstall.
2. **Completely**: GitHub → Settings → Developer settings → GitHub Apps →
   hermes-pr-review → Uninstall.

Revocation is immediate — any cached installation token stops working
within seconds (the next API call returns 401).
