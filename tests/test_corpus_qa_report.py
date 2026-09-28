"""Tests for scripts/corpus_qa_report.py — the acceptance rule and the
failure grouping that the weekly corpus QA issue is built from."""

from scripts.corpus_qa_report import (
    build_summary,
    group_failures,
    parse_failure_excerpts,
    verdict,
)


def _summary(score=80.0, cpc=0.0002, conversations=100):
    return {
        "score": score,
        "cost_per_conversation_usd": cpc,
        "conversations": conversations,
    }


BASELINE = {"score": 80.0, "cost_per_conversation_usd": 0.0002, "noise_pp": 5.0}


class TestVerdict:
    def test_no_baseline(self):
        assert verdict(_summary(), None)[0] == "no-baseline"

    def test_empty_run_is_failed_run(self):
        assert verdict(_summary(conversations=0), BASELINE)[0] == "failed-run"
        assert verdict(_summary(score=None), BASELINE)[0] == "failed-run"

    def test_within_noise_band_passes(self):
        assert verdict(_summary(score=75.1), BASELINE)[0] == "pass"

    def test_score_drop_beyond_noise_is_regression(self):
        v, reasons = verdict(_summary(score=74.9), BASELINE)
        assert v == "regression"
        assert "below baseline" in reasons[0]

    def test_cost_up_to_ten_percent_passes(self):
        assert verdict(_summary(cpc=0.00022), BASELINE)[0] == "pass"

    def test_cost_over_ten_percent_is_regression(self):
        v, reasons = verdict(_summary(cpc=0.000221), BASELINE)
        assert v == "regression"
        assert "Cost per conversation" in reasons[0]

    def test_missing_cost_does_not_fail_cost_rule(self):
        assert verdict(_summary(cpc=None), BASELINE)[0] == "pass"


def _cell(passed, total, status=None):
    return {
        "passed_count": passed,
        "total_count": total,
        "status": status or ("passed" if passed == total else "failed"),
    }


class TestGroupFailures:
    def test_maps_scenarios_to_modes(self):
        report = {
            "restaurants": [
                {
                    "name": "Pizzaria A",
                    "scenarios": {
                        "add_single": _cell(0, 1),
                        "checkout": _cell(0, 1),
                        "unavailable": _cell(0, 1),
                        "remove": _cell(0, 1),
                        "greeting": _cell(1, 1),
                        "question": _cell(0, 0, "skipped"),
                    },
                }
            ]
        }
        groups = group_failures(report)
        assert list(groups) == ["F7", "F9", "F10", "Other"]
        assert groups["F7"]["scenarios"] == {"add_single": ["Pizzaria A"]}
        assert groups["Other"]["scenarios"] == {"remove": ["Pizzaria A"]}

    def test_partial_pass_across_samples_is_flaky(self):
        report = {
            "restaurants": [
                {"name": "B", "scenarios": {"add_multi": _cell(2, 3)}},
            ]
        }
        groups = group_failures(report)
        assert list(groups) == ["F8"]


class TestParseExcerpts:
    def test_handles_short_and_long_headers(self):
        log = "\n".join(
            [
                "___ TestRandAddMulti.test_add_multi[Burger Place] ___",
                "tests/simulation/test_random_restaurants.py:200: in test_add_multi",
                "E   AssertionError: 'Coca 350ml' not in cart: []",
                "E   assert False",
                "_ TestRandRemove.test_remove[A very long restaurant name that fills the line] _",
                "E   AssertionError: 'X' still in cart",
            ]
        )
        ex = parse_failure_excerpts(log)
        assert ex["add_multi"] == [
            "Burger Place: AssertionError: 'Coca 350ml' not in cart: []"
        ]
        assert ex["remove"][0].startswith("A very long restaurant name")


class TestBuildSummary:
    def test_cost_per_conversation_excludes_skipped(self):
        report = {"total_tests": 42, "skipped": 2, "comprehension_pass_rate": 70.0}
        s = build_summary(report, {"total_usd": 0.004, "llm_calls": 10})
        assert s["conversations"] == 40
        assert s["cost_per_conversation_usd"] == 0.0001

    def test_no_cost_data(self):
        s = build_summary({"total_tests": 5, "skipped": 0}, None)
        assert s["cost_per_conversation_usd"] is None
