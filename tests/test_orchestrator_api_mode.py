import logging

from app.pipeline.orchestrator import Orchestrator


class _Client:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error

    def get_live_matches(self):
        if self.error is not None:
            raise self.error
        return self.result


def _orchestrator_with_client(client) -> Orchestrator:
    orchestrator = object.__new__(Orchestrator)
    orchestrator.client = client
    orchestrator.logger = logging.getLogger("test_orchestrator_api_mode")
    orchestrator.poll_iteration = 7
    return orchestrator


def test_api_poll_failure_keeps_loop_alive(caplog) -> None:
    orchestrator = _orchestrator_with_client(_Client(error=RuntimeError("api offline")))

    with caplog.at_level(logging.WARNING):
        matches = orchestrator._get_live_matches_for_poll()

    assert matches == []
    assert "keeping stream recorder alive" in caplog.text
    assert any(record.extra["error"] == "api offline" for record in caplog.records)


def test_api_poll_success_returns_matches() -> None:
    expected = [object()]
    orchestrator = _orchestrator_with_client(_Client(result=expected))

    assert orchestrator._get_live_matches_for_poll() == expected
