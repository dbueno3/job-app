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


@pytest.fixture(autouse=True)
def no_real_repo_git(monkeypatch):
    """Data-branch tests use throwaway repos; never touch this repo or its GitHub remote."""
    from jobbot import datasync

    real_git = datasync.DataBranch._git

    def guarded(self, *args, **kwargs):
        assert self.repo.resolve() != datasync.REPO_ROOT.resolve(), "test used the real repo"
        return real_git(self, *args, **kwargs)

    monkeypatch.setattr(datasync.DataBranch, "_git", guarded)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Polite delays and retry backoff are real sleeps; skip them in tests."""
    monkeypatch.setattr("time.sleep", lambda s: None)


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
