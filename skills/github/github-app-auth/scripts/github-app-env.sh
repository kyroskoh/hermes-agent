#!/usr/bin/env bash
# GitHub App environment setup for Hermes Agent skills.
#
# Usage:
#   source skills/github/github-app-auth/scripts/github-app-env.sh
#
# After sourcing, these variables are set (when GitHub App creds exist):
#   GH_AUTH_METHOD   "app" (overrides "gh" / "curl" / "none" when applicable)
#   GH_BOT_LOGIN     the GitHub App slug (e.g. "hermes-pr-review[bot]")
#   GITHUB_TOKEN     installation access token (same env var every skill uses)
#   GH_TOKEN         alias — gh CLI accepts either
#
# Behavior:
#   * If GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, and one of
#     GITHUB_APP_PRIVATE_KEY_PATH / GITHUB_APP_PRIVATE_KEY are set in env,
#     an installation token is minted (or reused from cache) and exported
#     into GITHUB_TOKEN / GH_TOKEN.
#   * If the GitHub App vars are NOT set, this script is a no-op so it can
#     safely be sourced from gh-env.sh without changing personal-auth flows.
#
# Re-run this script any time you need a fresh token (it auto-refreshes
# when the cached token is within 5 minutes of expiry).
#
# Exit behavior:
#   On configuration errors (missing vars, unreadable key), prints to stderr
#   and leaves GITHUB_TOKEN untouched — gh-env.sh then falls back to its
#   existing personal-token detection.

# Resolve the script's directory and source the token minter.
_HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
_APP_AUTH_DIR="$_HERMES_HOME/skills/github/github-app-auth/scripts"
_TOKEN_SCRIPT="$_APP_AUTH_DIR/github-app-token.py"

# Required env vars for app mode
if [ -z "${GITHUB_APP_ID:-}" ] || [ -z "${GITHUB_APP_INSTALLATION_ID:-}" ]; then
    # Not configured for app auth — leave existing GITHUB_TOKEN alone.
    return 0 2>/dev/null || true
    exit 0
fi

if [ ! -f "$_TOKEN_SCRIPT" ]; then
    echo "github-app-env: token script missing at $_TOKEN_SCRIPT" >&2
    return 0 2>/dev/null || true
    exit 0
fi

# Mint (or reuse cached) installation token
_APP_TOKEN="$(python3 "$_TOKEN_SCRIPT" 2>/dev/null)"
_APP_RC=$?
if [ "$_APP_RC" -ne 0 ] || [ -z "$_APP_TOKEN" ]; then
    # Don't clobber an existing token; just report the failure.
    echo "github-app-env: failed to mint installation token (rc=$_APP_RC)" >&2
    return 0 2>/dev/null || true
    exit 0
fi

# Export — every downstream tool that reads GITHUB_TOKEN or GH_TOKEN
# will now act as the App bot, not the personal account.
export GITHUB_TOKEN="$_APP_TOKEN"
export GH_TOKEN="$_APP_TOKEN"

# Surface a hint of the bot identity (the App slug is not part of the
# standard token introspection response, so we derive it from the App name
# in env if the operator set GITHUB_APP_NAME, or just advertise "app").
GH_BOT_LOGIN="${GITHUB_APP_NAME:-app}[bot]"
export GH_BOT_LOGIN

echo "GitHub Auth: app"
echo "Bot: $GH_BOT_LOGIN (installation ${GITHUB_APP_INSTALLATION_ID})"
