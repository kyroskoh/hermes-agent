#!/usr/bin/env bash
# GitHub environment detection helper for Hermes Agent skills.
#
# Usage (via terminal tool):
#   source skills/github/github-auth/scripts/gh-env.sh
#
# After sourcing, these variables are set:
#   GH_AUTH_METHOD  - "app", "gh", "curl", or "none"
#   GITHUB_TOKEN    - token used by every GitHub API call (App, PAT, or gh-derived)
#   GH_TOKEN        - alias of GITHUB_TOKEN for the gh CLI
#   GH_USER         - GitHub login (user, or "<app-name>[bot]" for App mode)
#   GH_BOT_LOGIN    - alias of GH_USER, reserved for bot-attributed actions
#   GH_OWNER        - repo owner  (only if inside a git repo with a github remote)
#   GH_REPO         - repo name   (only if inside a git repo with a github remote)
#   GH_OWNER_REPO   - owner/repo  (only if inside a git repo with a github remote)
#
# Auth precedence (first match wins):
#   1. GitHub App credentials present (GITHUB_APP_ID + INSTALLATION_ID + key)
#      → mint an installation access token, attribute actions to the App bot
#   2. gh CLI already authenticated → use gh for everything
#   3. GITHUB_TOKEN (env) / ~/.hermes/.env / ~/.git-credentials → curl + token
#   4. No credentials → GH_AUTH_METHOD=none

# --- GitHub App mode (highest priority) ---
# Auto-source the App env helper if the App creds are configured. This
# exports GH_TOKEN/GITHUB_TOKEN as an installation access token BEFORE
# the gh / PAT fallback paths run, so every downstream `gh pr review`,
# `gh pr comment`, `gh api ...`, and raw `curl` against api.github.com
# authenticates as the App bot.
if [ -z "${HERMES_PR_REVIEW_USE_PERSONAL:-}" ]; then
    _app_env="${HERMES_HOME:-$HOME/.hermes}/skills/github/github-app-auth/scripts/github-app-env.sh"
    if [ -f "$_app_env" ]; then
        # shellcheck disable=SC1090
        source "$_app_env" || true
    fi
    unset _app_env
fi

# --- Auth detection ---

GH_AUTH_METHOD="none"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
GH_USER=""
GH_BOT_LOGIN="${GH_BOT_LOGIN:-}"

# Detect App mode: token script exported GH_BOT_LOGIN
if [ -n "$GH_BOT_LOGIN" ]; then
    GH_AUTH_METHOD="app"
    GH_USER="$GH_BOT_LOGIN"
elif command -v gh &>/dev/null && [ -z "$GITHUB_TOKEN" ] && gh auth status &>/dev/null 2>&1; then
    GH_AUTH_METHOD="gh"
    GH_USER=$(gh api user --jq '.login' 2>/dev/null)
elif [ -n "$GITHUB_TOKEN" ]; then
    GH_AUTH_METHOD="curl"
elif _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env" 2>/dev/null; then
    GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
    if [ -n "$GITHUB_TOKEN" ]; then
        GH_AUTH_METHOD="curl"
    fi
elif [ -f "$HOME/.git-credentials" ]; then
    GITHUB_TOKEN=$(uv run python3 "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/git-credential-token.py")
    if [ -n "$GITHUB_TOKEN" ]; then
        GH_AUTH_METHOD="curl"
    fi
fi

# gh CLI accepts both GH_TOKEN and GITHUB_TOKEN; mirror them so neither
# downstream `gh pr review` nor raw `curl` can miss the App identity.
export GH_TOKEN="$GITHUB_TOKEN"

# Resolve username for curl method
if [ "$GH_AUTH_METHOD" = "curl" ] && [ -z "$GH_USER" ]; then
    GH_USER=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
        https://api.github.com/user 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('login',''))" 2>/dev/null)
fi

# --- Repo detection (if inside a git repo with a GitHub remote) ---

GH_OWNER=""
GH_REPO=""
GH_OWNER_REPO=""

_remote_url=$(git remote get-url origin 2>/dev/null)
if [ -n "$_remote_url" ] && echo "$_remote_url" | grep -q "github.com"; then
    GH_OWNER_REPO=$(echo "$_remote_url" | sed -E 's|.*github\.com[:/]||; s|\.git$||')
    GH_OWNER=$(echo "$GH_OWNER_REPO" | cut -d/ -f1)
    GH_REPO=$(echo "$GH_OWNER_REPO" | cut -d/ -f2)
fi
unset _remote_url

# --- Summary ---

echo "GitHub Auth: $GH_AUTH_METHOD"
[ -n "$GH_USER" ]       && echo "User: $GH_USER"
[ -n "$GH_OWNER_REPO" ] && echo "Repo: $GH_OWNER_REPO"
[ "$GH_AUTH_METHOD" = "none" ] && echo "⚠ Not authenticated — see github-auth skill"

export GH_AUTH_METHOD GITHUB_TOKEN GH_TOKEN GH_USER GH_BOT_LOGIN GH_OWNER GH_REPO GH_OWNER_REPO
