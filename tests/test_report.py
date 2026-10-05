import json

from nycynapse.evals.report import readme_table


def test_table_from_published_summaries(tmp_path):
    summary = {"cases": 55, "overall_correct": 0.745, "cost_usd": 3.3, "latency_ms_p50": 4864}
    for key in ("e0", "e1"):
        (tmp_path / f"holdout-{key}.json").write_text(
            json.dumps({"id": key, "split": "holdout", "snapshot": 2298, "summary": summary})
        )
    table = readme_table(tmp_path)
    assert "| | E0 | E1 |" in table
    assert "| Right answer | 74.5% | 74.5% |" in table
    assert "| Model cost per question | $0.060 | $0.060 |" in table
    assert "snapshot 2298" in table


def test_no_results_yet(tmp_path):
    assert readme_table(tmp_path) == "Results are being produced.\n"
