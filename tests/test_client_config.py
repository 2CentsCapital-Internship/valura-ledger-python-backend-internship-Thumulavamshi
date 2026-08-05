"""The two client gaps that could waste a SCORED attempt.

Not transport tests -- configuration tests. G-1 and G-2 are cheap to get wrong
and expensive to discover during a 60-minute submission run.
"""
import pytest

from client import (MODE_DURATION, TAIL_MARGIN_SECONDS, ArenaClient,
                    run_ceiling)


class TestRunCeiling:
    """G-1: the starter kit defaulted to 1500s (25 min) for every mode."""

    @pytest.mark.parametrize("mode,nominal", [
        ("practice", 1200), ("submission", 3600), ("final", 4500),
    ])
    def test_ceiling_clears_the_nominal_duration(self, mode, nominal):
        assert run_ceiling(mode, {}) == nominal + TAIL_MARGIN_SECONDS
        assert run_ceiling(mode, {}) > nominal

    def test_old_default_would_have_truncated_scored_runs(self):
        """The exact bug: 1500s against a 3,600s submission and a 4,500s final."""
        assert 1500 < MODE_DURATION["submission"]
        assert 1500 < MODE_DURATION["final"]

    def test_live_rules_win_over_the_local_table(self):
        """"If this table and that endpoint ever disagree, the endpoint wins.\""""
        rules = {"modes": {"final": {"duration_seconds": 5400}}}
        assert run_ceiling("final", rules) == 5400 + TAIL_MARGIN_SECONDS

    def test_malformed_rules_fall_back_safely(self):
        for rules in ({}, {"modes": {}}, {"modes": {"final": {}}},
                      {"modes": {"final": {"duration_seconds": 0}}},
                      {"modes": {"final": {"duration_seconds": None}}}):
            assert run_ceiling("final", rules) == 4500 + TAIL_MARGIN_SECONDS


class TestNewRunFlag:
    """G-2: &new=true was never sent, and must never be sent on a reconnect."""

    def _client(self, **kw):
        return ArenaClient("https://example.invalid", "ak_test", "submission", **kw)

    def test_not_sent_by_default(self):
        c = self._client()
        assert "new" not in c._stream_params()

    def test_sent_on_the_first_connect_when_asked(self):
        c = self._client(start_new=True)
        assert c._stream_params()["new"] == "true"

    def test_never_sent_on_a_reconnect(self):
        """A mid-run reconnect carrying new=true would spend an attempt."""
        c = self._client(start_new=True)
        assert "new" in c._stream_params()
        c.first_connect = False                      # a reconnect happened
        assert "new" not in c._stream_params()

    def test_cursor_is_always_sent(self):
        """"Always send from; the server may rewind you.\""""
        c = self._client()
        c.cursor = 4213
        assert c._stream_params()["from"] == 4213


class TestPostingBatchLimit:
    def test_never_exceeds_five_hundred_per_request(self):
        from client import MAX_POSTINGS_PER_REQUEST
        assert MAX_POSTINGS_PER_REQUEST == 500
