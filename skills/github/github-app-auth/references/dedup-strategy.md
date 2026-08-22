# Deduplication Strategy for `synchronize` Events

## Why

When GitHub re-fires the `pull_request` webhook with `action: synchronize`
(a new commit push on the same PR), the naive agent will re-post the same
review. To avoid this, we fingerprint each review and skip when the
fingerprint is unchanged.

## What a fingerprint is

A SHA-256 hash over the sorted list of `(path, line, body)` triples in
the review. Body is whitespace-stripped so cosmetic diff doesn't change
the fingerprint.

Implementation: `scripts/pr-review-dedup.py`.

## When to record

After the agent posts a review (inline comments + summary comment +
approve/request-changes event), it calls:

```bash
python3 pr-review-dedup.py record \
  --owner "$GH_OWNER" --repo "$GH_REPO" --pr "$PR_NUMBER" \
  --head-sha "$HEAD_SHA" --verdict "$VERDICT" \
  --findings-json "$FINDINGS_JSON"
```

## When to skip

At the start of the review (before running any diff analysis), the
agent checks:

```bash
if python3 pr-review-dedup.py is-duplicate \
     --owner "$GH_OWNER" --repo "$GH_REPO" --pr "$PR_NUMBER" \
     --head-sha "$HEAD_SHA" \
     --findings-json "$NEW_FINDINGS_JSON"; then
  echo "Same findings as the previous review — skipping."
  exit 0
fi
```

`is-duplicate` returns exit 0 only if the **same fingerprint** is already
stored for this `(owner, repo, pr)`.

## Edge cases

| Scenario | Behavior |
|----------|----------|
| New PR, never reviewed | No prior fingerprint; is-duplicate returns 1 → review normally |
| Push of identical diff (force-push of same SHA) | Fingerprint unchanged → skip |
| Push of new code (new SHA, different diff) | Fingerprint changes → review normally |
| Push of same diff but new SHA (e.g. amend) | Fingerprint unchanged → skip (good — no new findings) |
| PR reopened with new commits | New SHA → fingerprint recomputed from scratch |
| Hermes scope expansion finds new issue on old code | New findings → different fingerprint → review normally |

## Storage

Per-PR state is at:

```
$HERMES_HOME/.cache/pr-review/<owner>/<repo>/<pr>.json
```

with the schema:

```json
{
  "head_sha": "abc123...",
  "fingerprint": "sha256...",
  "verdict": "APPROVE | REQUEST_CHANGES | COMMENT",
  "finding_count": 3,
  "updated_at": 1787368034
}
```

File mode `0600` (no sensitive data — just the fingerprint and counts).
Atomic write via `os.replace` to prevent half-written state.

## Failure modes

If `pr-review-dedup.py` crashes or the cache is unreadable, the helper
returns "not a duplicate" — the agent proceeds to review normally. Worst
case: one duplicate review. Never a missed review.

To reset all dedup state (e.g. after a major code-review guideline change):

```bash
rm -rf ~/.hermes/.cache/pr-review
```

## Why not use the GitHub `last_review_commit_id` field?

GitHub stores `last_review_commit_id` on each review object. We could
compare against that and skip when it's the same SHA. We chose the
fingerprint approach instead because:

1. It catches the case where the **same SHA** has different findings
   (e.g. the agent's first review was buggy and missed something — on
   retry we want the corrected review).
2. It survives `dismiss` events (when an existing review is dismissed,
   GitHub wipes `last_review_commit_id`).
3. It's local-only — no extra API calls.

The two approaches are complementary; pick fingerprint for cheap
short-circuit and GitHub's field for the canonical record.
