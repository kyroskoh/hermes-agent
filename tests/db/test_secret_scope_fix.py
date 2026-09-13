"""Standalone tests for the deferred check_fn / per-profile secret scope fix.

Implements spec section N (multi-profile secret scope) regression tests.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "/usr/local/lib/hermes-agent")

# These tests exercise ``tools/registry.py`` which depends on the full
# Hermes stack. Import order matters because some modules cache state at
# import time.
from tools.registry import (  # noqa: E402
    CHECK_FN_UNRESOLVED,
    _check_fn_cached,
)


def _force_multiplex_and_no_override(monkeypatch):
    """Set up the conditions for ``CHECK_FN_UNRESOLVED``: multiplex active
    AND no HERMES_HOME override."""
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
    monkeypatch.setattr("hermes_constants.get_hermes_home_override",
                        lambda: None)


def _force_multiplex_with_override(monkeypatch, path):
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
    monkeypatch.setattr("hermes_constants.get_hermes_home_override",
                        lambda: path)


class CheckFnScopeTests(unittest.TestCase):
    def test_unresolved_returns_none_not_call(self):
        """Section N: when multiplex is active but no profile override is
        installed, ``_check_fn_cached`` returns ``None`` without calling
        the probe. This is the fix for the UnscopedSecretError that
        tools like ``check_discord_tool_requirements`` would otherwise
        raise."""
        import tools.registry as reg
        # Clear any cached verdicts from previous tests.
        reg.invalidate_check_fn_cache()

        def probe():
            raise AssertionError(
                "probe MUST NOT be called when profile scope is unresolved")

        with mock.patch.object(reg, "check_fn_cache_scope",
                                return_value=CHECK_FN_UNRESOLVED):
            verdict = _check_fn_cached(probe)
            self.assertIsNone(verdict,
                              f"expected None, got {verdict!r}")

    def test_resolved_calls_probe(self):
        import tools.registry as reg
        reg.invalidate_check_fn_cache()

        def probe():
            return True

        sentinel = "/tmp/this-profile"
        with mock.patch.object(reg, "check_fn_cache_scope",
                                return_value=sentinel):
            verdict = _check_fn_cached(probe)
            self.assertTrue(verdict)

    def test_unresolved_does_not_pollute_per_profile_cache(self):
        """A None-verdict for an unresolved scope MUST NOT cause a
        False-cached verdict for a later, resolved profile."""
        import tools.registry as reg
        reg.invalidate_check_fn_cache()

        def probe():
            return True

        # First call: unresolved → returns None without caching anything.
        with mock.patch.object(reg, "check_fn_cache_scope",
                                return_value=CHECK_FN_UNRESOLVED):
            v = _check_fn_cached(probe)
            self.assertIsNone(v)
        # Second call: resolved → returns True and caches the True verdict.
        with mock.patch.object(reg, "check_fn_cache_scope",
                                return_value="/tmp/profile-A"):
            v = _check_fn_cached(probe)
            self.assertTrue(v)
            # And the cache key was actually used.
            self.assertIn((probe, "/tmp/profile-A"),
                          reg._check_fn_cache)
            # The unresolved bucket must NOT contain a poisoned verdict.
            self.assertNotIn((probe, CHECK_FN_UNRESOLVED),
                             reg._check_fn_cache)


if __name__ == "__main__":
    unittest.main(verbosity=2)
