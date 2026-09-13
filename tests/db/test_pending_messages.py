"""Standalone tests for ``agent.pending_messages``.

Covers spec section P (durable queue + idempotent replay).
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/hermes-agent")

from agent import pending_messages as pm


class PendingMessageTests(unittest.TestCase):
    def test_enqueue_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            m = pm.PendingMessage.new(
                platform="telegram", profile="kyros",
                sender="alice", platform_message_id="m1",
                body="hello",
            )
            path = pm.enqueue(td, m)
            self.assertTrue(path.exists())
            self.assertEqual(len(list(Path(td).glob("pending_messages/*.json"))), 1)

    def test_dedup_key_deterministic(self):
        k1 = pm.PendingMessage.compute_dedup_key("tg", "kyros", "alice", "m1")
        k2 = pm.PendingMessage.compute_dedup_key("tg", "kyros", "alice", "m1")
        self.assertEqual(k1, k2)
        k3 = pm.PendingMessage.compute_dedup_key("tg", "wirbel", "alice", "m1")
        self.assertNotEqual(k1, k3)

    def test_list_pending_orders_by_received_at(self):
        with tempfile.TemporaryDirectory() as td:
            for i, body in enumerate(["a", "b", "c"]):
                m = pm.PendingMessage.new(
                    platform="discord", profile="kyros",
                    sender="bob", platform_message_id=f"m{i}",
                    body=body,
                )
                pm.enqueue(td, m)
                time.sleep(0.01)
            msgs = pm.list_pending(td)
            self.assertEqual([m.body for m in msgs], ["a", "b", "c"])

    def test_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            m = pm.PendingMessage.new(
                platform="whatsapp", profile="kyros",
                sender="carl", platform_message_id="wx1",
                body="replay-me",
            )
            pm.enqueue(td, m)
            calls = []

            def proc(msg):
                calls.append(msg.platform_message_id)
                return {"ok": True, "duplicate": False}

            summary = pm.replay_pending(td, process_fn=proc)
            self.assertEqual(summary["replayed"], 1)
            self.assertEqual(calls, ["wx1"])
            # Second replay should re-call proc (because state was set to
            # replayed and skip_state defaults to skip those), but no new
            # processing — the contract is "replay the queued; replayed
            # ones are skipped".
            calls.clear()
            summary2 = pm.replay_pending(td, process_fn=proc)
            self.assertEqual(summary2["skipped"], 1)
            self.assertEqual(calls, [])

    def test_duplicate_detected(self):
        with tempfile.TemporaryDirectory() as td:
            m = pm.PendingMessage.new(
                platform="telegram", profile="kyros",
                sender="alice", platform_message_id="dup",
                body="hi",
            )
            pm.enqueue(td, m)

            def proc(msg):
                return {"ok": True, "duplicate": True}

            summary = pm.replay_pending(td, process_fn=proc)
            self.assertEqual(summary["duplicates"], 1)

    def test_failure_leaves_message_in_failed_state(self):
        with tempfile.TemporaryDirectory() as td:
            m = pm.PendingMessage.new(
                platform="discord", profile="kyros",
                sender="alice", platform_message_id="f1",
                body="will-fail",
            )
            pm.enqueue(td, m)

            def proc(msg):
                raise RuntimeError("downstream unavailable")

            summary = pm.replay_pending(td, process_fn=proc)
            self.assertEqual(summary["failed"], 1)
            msgs = pm.list_pending(td, state="failed")
            self.assertEqual(len(msgs), 1)
            self.assertIn("downstream", msgs[0].last_error)

    def test_atomic_write_survives_concurrent_writers(self):
        """The atomic tmp+fsync+rename recipe survives concurrent
        enqueues without producing half-written files."""
        with tempfile.TemporaryDirectory() as td:
            errors: list[str] = []

            def worker(idx: int):
                try:
                    for i in range(20):
                        m = pm.PendingMessage.new(
                            platform="test", profile=f"p{idx}",
                            sender=f"s{idx}", platform_message_id=f"m{i}",
                            body=f"w{idx}-{i}",
                        )
                        pm.enqueue(td, m)
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            msgs = pm.list_pending(td)
            self.assertEqual(len(msgs), 8 * 20)
            # Ensure every file parses (no half-written).
            for entry in Path(td, "pending_messages").glob("pending-*.json"):
                blob = entry.read_text(encoding="utf-8")
                self.assertGreater(len(blob), 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
