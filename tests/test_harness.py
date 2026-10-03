import pytest

from nycynapse.evals.cases import Case
from nycynapse.evals.harness import evaluate
from nycynapse.evals.system import Answer
from nycynapse.llm.client import ProviderLimit

CASES = [Case(id=f"x-{i}", question=f"q{i}", category="unsupported",
              expect={"classification": "unsupported"}) for i in range(3)]


class Flaky:
    name = "flaky"

    def __init__(self, fail_at):
        self.fail_at, self.calls = fail_at, 0

    def answer(self, question, as_of):
        self.calls += 1
        if self.calls == self.fail_at:
            raise ProviderLimit("session limit")
        return Answer("unsupported")


def test_provider_limit_stops_and_resume_continues(tmp_path):
    out = tmp_path / "run.json"
    with pytest.raises(SystemExit, match="provider limit"):
        evaluate(Flaky(fail_at=2), CASES, None, out, meta={}, progress=lambda m: None)
    assert len(out.with_suffix(".partial.jsonl").read_text().splitlines()) == 1

    system = Flaky(fail_at=0)
    summary = evaluate(system, CASES, None, out, meta={}, progress=lambda m: None)
    assert system.calls == 2  # the finished case was not asked again
    assert summary["cases"] == 3 and summary["overall_correct"] == 1.0
    assert not out.with_suffix(".partial.jsonl").exists()
