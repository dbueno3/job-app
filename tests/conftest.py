import json
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test tries to reach a live endpoint."""

    def blocked(self, request):
        raise RuntimeError(f"tests must not hit the network: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def mock_client(routes: dict[str, httpx.Response | Exception]) -> httpx.Client:
    """A client whose responses are keyed by URL path (e.g. '/v1/boards/stripe/jobs')."""

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = routes.get(request.url.path, httpx.Response(404))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return httpx.Client(transport=httpx.MockTransport(handler))
