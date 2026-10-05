from nycynapse.api.service import PAUSED, Answerer
from nycynapse.llm.client import ProviderLimit


class _Graph:
    def stream(self, *_args, **_kwargs):
        raise ProviderLimit("session limit")
        yield  # pragma: no cover


def test_provider_limit_pauses_questions_and_stores_nothing():
    a = Answerer.__new__(Answerer)
    a.graph = _Graph()
    a.ctx = None  # storing a trace would fail on this
    events = list(a.stream("How many rides?", "client"))
    assert events == [("error", {"message": PAUSED})]
    assert a.paused()
