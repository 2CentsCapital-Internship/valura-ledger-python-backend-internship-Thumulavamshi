"""The two client gaps that could waste a SCORED attempt.

Not transport tests -- configuration tests. G-1 and G-2 are cheap to get wrong
and expensive to discover during a 60-minute submission run.
"""
import pytest

from client import (MODE_DURATION, TAIL_MARGIN_SECONDS, ArenaClient,
                    confirm_scored_run, normalise_confirmation, run_ceiling)


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


class TestScoredRunConfirmation:
    """The guard on a scarce resource: 3 submission attempts, 1 final.

    Added after the prompt failed to display under a `2>` stderr redirect --
    input()'s prompt argument goes through the C-level readline path and was
    swallowed, leaving a bare cursor with no instruction. Typing 'y' at it
    cancelled the run. No attempt was lost, but only because the confirmation
    sits before any /v1/stream call.
    """

    @pytest.mark.parametrize("typed", ["submission", " submission ",
                                       "﻿submission", "submission\n"])
    def test_exact_mode_name_proceeds(self, typed, capsys):
        assert confirm_scored_run("submission", 4500, True,
                                  read_line=lambda: typed) is True

    @pytest.mark.parametrize("typed", ["y", "yes", "", "Submission",
                                       "final", "n", "  "])
    def test_anything_else_cancels(self, typed, capsys):
        assert confirm_scored_run("submission", 4500, True,
                                  read_line=lambda: typed) is False
        out = capsys.readouterr().out
        assert "NO ATTEMPT WAS CONSUMED" in out

    def test_eof_cancels_rather_than_crashing(self, capsys):
        def raise_eof():
            raise EOFError
        assert confirm_scored_run("final", 5400, True,
                                  read_line=raise_eof) is False

    def test_the_required_word_is_printed_not_just_prompted(self, capsys):
        """The whole point: the instruction must survive a stderr redirect, so
        it has to go through print(), not input()'s prompt argument."""
        confirm_scored_run("submission", 4500, True, read_line=lambda: "y")
        out = capsys.readouterr().out
        assert "Type exactly:  submission" in out
        assert "WILL count" in out

    def test_bom_does_not_reject_a_correct_answer(self):
        assert normalise_confirmation("﻿submission") == "submission"


class TestPostingBatchLimit:
    def test_never_exceeds_five_hundred_per_request(self):
        from client import MAX_POSTINGS_PER_REQUEST
        assert MAX_POSTINGS_PER_REQUEST == 500
