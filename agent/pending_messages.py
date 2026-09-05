"""Durable pending-message queue with idempotent replay.

Implements Section P of the state-db-reliability design.

Inbound platform messages are durably enqueued in
``<state_dir>/pending_messages/pending-<uuid>.json`` BEFORE any database
write. The atomic write uses the standard tmp+fsync+rename recipe so a
power loss cannot produce a half-written message.

Replay is idempotent on ``(platform, profile, sender, platform_message_id)``;
the same inbound message never gets inserted into the canonical messages
table twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


PENDING_DIRNAME = "pending_messages"


@dataclass
class PendingMessage:
    """One durable pending message.

    The dedup key is computed from the four-tuple (platform, profile,
    sender, platform_message_id) and stored in the ``dedup_key`` field so
    replay can detect duplicates without recomputing.
    """
    id: str
    platform: str
    profile: str
    sender: str
    platform_message_id: str
    received_at: float
    body: str
    dedup_key: str
    state: str = "queued"  # queued / replaying / replayed / failed
    attempt_count: int = 0
    last_attempt_at: Optional[float] = None
    last_error: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @staticmethod
    def compute_dedup_key(platform: str, profile: str, sender: str,
                          platform_message_id: str) -> str:
        h = hashlib.sha256()
        for part in (platform, profile, sender, platform_message_id):
            h.update(part.encode("utf-8"))
            h.update(b"|")
        return h.hexdigest()

    @classmethod
    def new(cls, *, platform: str, profile: str, sender: str,
            platform_message_id: str, body: str,
            extra: Optional[dict] = None) -> "PendingMessage":
        return cls(
            id=str(uuid.uuid4()),
            platform=platform,
            profile=profile,
            sender=sender,
            platform_message_id=platform_message_id,
            received_at=time.time(),
            body=body,
            dedup_key=cls.compute_dedup_key(platform, profile, sender,
                                            platform_message_id),
            extra=extra or {},
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, blob: dict) -> "PendingMessage":
        return cls(
            id=blob["id"],
            platform=blob["platform"],
            profile=blob["profile"],
            sender=blob["sender"],
            platform_message_id=blob["platform_message_id"],
            received_at=blob["received_at"],
            body=blob["body"],
            dedup_key=blob["dedup_key"],
            state=blob.get("state", "queued"),
            attempt_count=blob.get("attempt_count", 0),
            last_attempt_at=blob.get("last_attempt_at"),
            last_error=blob.get("last_error"),
            extra=blob.get("extra", {}),
        )


def pending_dir(state_dir: os.PathLike) -> Path:
    p = Path(state_dir).expanduser().resolve() / PENDING_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def enqueue(state_dir: os.PathLike, message: PendingMessage) -> Path:
    """Write the message to disk atomically. Returns the path written."""
    d = pending_dir(state_dir)
    dest = d / f"pending-{message.id}.json"
    tmp = d / f".pending-{message.id}.json.tmp"
    tmp.write_text(json.dumps(message.to_dict(), indent=2, sort_keys=True),
                   encoding="utf-8")
    # fsync before rename so the data is durable on disk.
    fd = os.open(str(tmp), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dest)
    # fsync the directory entry.
    dfd = os.open(str(d), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    logger.debug("pending message enqueued: id=%s dedup=%s", message.id,
                 message.dedup_key[:12])
    return dest


def list_pending(state_dir: os.PathLike,
                 *,
                 state: Optional[str] = None) -> list[PendingMessage]:
    """List pending messages, optionally filtered by state. Sorted by
    received_at ascending so the replay path processes in order.
    """
    d = pending_dir(state_dir)
    out: list[PendingMessage] = []
    for entry in sorted(d.glob("pending-*.json")):
        try:
            blob = json.loads(entry.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("pending list: skipping unparseable %s: %s", entry, e)
            continue
        m = PendingMessage.from_dict(blob)
        if state is None or m.state == state:
            out.append(m)
    out.sort(key=lambda m: m.received_at)
    return out


def _atomic_state_write(path: Path, message: PendingMessage) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(message.to_dict(), indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def replay_pending(state_dir: os.PathLike, *,
                   process_fn=None,
                   mark_replayed: bool = True,
                   skip_state: Iterable[str] = ("replayed",)) -> dict:
    """Replay every queued message through ``process_fn``.

    ``process_fn`` is a callable receiving a ``PendingMessage`` and
    returning a dict with at least ``{"ok": bool, "duplicate": bool}``.
    When ``duplicate`` is True, the entry is marked as replayed without
    raising. When ``ok`` is False and not duplicate, the entry is left in
    state ``replaying`` with ``last_error`` populated; the next replay
    retries it (idempotency contract: ``process_fn`` MUST itself dedup on
    ``(platform, profile, sender, platform_message_id)`` before inserting
    into the messages table).

    Returns a summary ``{"replayed": int, "duplicates": int, "failed": int}``.
    """
    summary = {"replayed": 0, "duplicates": 0, "failed": 0,
               "skipped": 0, "errors": []}
    d = pending_dir(state_dir)
    skip = set(skip_state)
    for entry in sorted(d.glob("pending-*.json")):
        try:
            blob = json.loads(entry.read_text(encoding="utf-8"))
        except Exception as e:
            summary["errors"].append({"file": str(entry), "error": str(e)})
            continue
        msg = PendingMessage.from_dict(blob)
        if msg.state in skip:
            summary["skipped"] += 1
            continue
        msg.state = "replaying"
        msg.attempt_count += 1
        msg.last_attempt_at = time.time()
        _atomic_state_write(entry, msg)
        if process_fn is None:
            # No processing function — leave in 'replaying' state.
            summary["skipped"] += 1
            continue
        try:
            res = process_fn(msg) or {}
            ok = bool(res.get("ok"))
            dup = bool(res.get("duplicate"))
        except Exception as e:
            msg.state = "failed"
            msg.last_error = str(e)
            _atomic_state_write(entry, msg)
            summary["failed"] += 1
            summary["errors"].append({"id": msg.id, "error": str(e)})
            continue
        if dup:
            msg.state = "replayed"
            msg.last_error = "duplicate"
            if mark_replayed:
                _atomic_state_write(entry, msg)
            summary["duplicates"] += 1
            continue
        if ok:
            msg.state = "replayed"
            msg.last_error = None
            if mark_replayed:
                _atomic_state_write(entry, msg)
            summary["replayed"] += 1
        else:
            msg.state = "failed"
            msg.last_error = "process_fn returned ok=False"
            _atomic_state_write(entry, msg)
            summary["failed"] += 1
    return summary


__all__ = [
    "PendingMessage",
    "pending_dir",
    "enqueue",
    "list_pending",
    "replay_pending",
]
