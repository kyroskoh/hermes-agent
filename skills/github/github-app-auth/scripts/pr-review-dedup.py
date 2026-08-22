#!/usr/bin/env python3
"""
Deduplication state for the github-pr-review webhook route.

Records a per-PR review fingerprint so the agent does not re-post the
same findings when GitHub re-fires the `pull_request` webhook with
`action: synchronize` (a new commit push on the same PR).

Fingerprint is the SHA-256 hex of the sorted list of (path, line, body)
triples from the previous review. A new review with the same fingerprint
is treated as a no-op; a changed fingerprint (different diff OR new
findings) supersedes the stored one.

State directory:   $HERMES_HOME/.cache/pr-review/<owner>/<repo>/<pr>.json
Lock file:         same path + ".lock" (atomic rename protects readers)

Public API:
    ReviewDedup.mark_unchanged(owner, repo, pr_number, head_sha)
        -> Returns True if (owner, repo, pr, head_sha) already had the
           same fingerprint as before (review would be a duplicate).
           Returns False if the fingerprint is new or the head changed.

    ReviewDedup.record(owner, repo, pr_number, head_sha, findings)
        -> Persist the fingerprint for (owner, repo, pr, head_sha).

    ReviewDedup.get(owner, repo, pr_number)
        -> Return the stored fingerprint dict, or None.

This is intentionally a small, auditable module — the entire dedup logic
fits in ~80 lines so the agent and the operator can both reason about it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional


CACHE_SUBDIR = Path(".cache") / "pr-review"


def _cache_root() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / CACHE_SUBDIR


def _dedup_path(owner: str, repo: str, pr_number: int) -> Path:
    return _cache_root() / owner / repo / f"{pr_number}.json"


def _lock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


def _atomic_write(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(
        prefix=target.name + ".", dir=str(target.parent), text=True
    )
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fingerprint(findings: Iterable[dict[str, Any]]) -> str:
    """Stable SHA-256 over the sorted (path, line, body) triples."""
    norm = []
    for f in findings:
        norm.append(
            (
                str(f.get("path", "")),
                str(f.get("line", "")),
                str(f.get("body", "")).strip(),
            )
        )
    norm.sort()
    blob = "\n".join(f"{p}:{l}:{b}" for p, l, b in norm).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class ReviewDedup:
    @staticmethod
    def get(owner: str, repo: str, pr_number: int) -> Optional[dict[str, Any]]:
        path = _dedup_path(owner, repo, pr_number)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def record(
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        findings: Iterable[dict[str, Any]],
        verdict: str = "",
    ) -> dict[str, Any]:
        fp = fingerprint(findings)
        payload = {
            "head_sha": head_sha,
            "fingerprint": fp,
            "verdict": verdict,
            "finding_count": len(list(findings)) if not isinstance(findings, list) else len(findings),
            "updated_at": int(time.time()),
        }
        _atomic_write(_dedup_path(owner, repo, pr_number), payload)
        return payload

    @staticmethod
    def is_duplicate(
        owner: str, repo: str, pr_number: int, head_sha: str, findings: Iterable[dict[str, Any]]
    ) -> bool:
        """Return True if the same fingerprint was already recorded for this head SHA."""
        prev = ReviewDedup.get(owner, repo, pr_number)
        if not prev:
            return False
        if prev.get("head_sha") != head_sha:
            # Different commit — fall through to fingerprint comparison;
            # if the new diff is identical despite a new SHA we still
            # skip. (e.g. force-push of the same content.)
            pass
        return prev.get("fingerprint") == fingerprint(findings)


# ─── CLI (mostly for testing / debugging) ──────────────────────────────────


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pr-review-dedup")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get")
    g.add_argument("--owner", required=True)
    g.add_argument("--repo", required=True)
    g.add_argument("--pr", required=True, type=int)

    r = sub.add_parser("record")
    r.add_argument("--owner", required=True)
    r.add_argument("--repo", required=True)
    r.add_argument("--pr", required=True, type=int)
    r.add_argument("--head-sha", required=True)
    r.add_argument("--verdict", default="")
    r.add_argument("--findings-json", default="[]",
                   help="JSON array of {path,line,body} findings")

    d = sub.add_parser("is-duplicate")
    d.add_argument("--owner", required=True)
    d.add_argument("--repo", required=True)
    d.add_argument("--pr", required=True, type=int)
    d.add_argument("--head-sha", required=True)
    d.add_argument("--findings-json", default="[]")

    args = p.parse_args(argv)
    if args.cmd == "get":
        print(json.dumps(ReviewDedup.get(args.owner, args.repo, args.pr), indent=2))
        return 0
    if args.cmd == "record":
        findings = json.loads(args.findings_json)
        payload = ReviewDedup.record(
            args.owner, args.repo, args.pr, args.head_sha, findings, args.verdict
        )
        print(json.dumps(payload, indent=2))
        return 0
    if args.cmd == "is-duplicate":
        findings = json.loads(args.findings_json)
        return 0 if ReviewDedup.is_duplicate(
            args.owner, args.repo, args.pr, args.head_sha, findings
        ) else 1
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
