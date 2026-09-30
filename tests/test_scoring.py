from datetime import UTC, datetime, timedelta

import pytest

from jobbot.config import load_filters
from jobbot.models import Match
from jobbot.scoring import Scorer, sort_matches
from tests.test_filters import make_job

CFG = load_filters()
SCORER = Scorer(CFG.scoring)


def test_title_hits_weigh_more_than_description():
    in_title = make_job(title="Backend Engineer, Payments", description_text="")
    in_desc = make_job(title="Backend Engineer", description_text="Work on payments.")
    assert SCORER.score(in_title)[0] == 3
    assert SCORER.score(in_desc)[0] == 1


def test_keyword_in_both_title_and_description_counts_both():
    job = make_job(title="Software Engineer, Payments", description_text="payments infra")
    assert SCORER.score(job) == (4, ["payments"])


def test_repeated_keyword_counts_once():
    job = make_job(description_text="Python. Python. Python. Python.")
    assert SCORER.score(job)[0] == 1


def test_multiple_keywords():
    job = make_job(
        description_text="We use Python, Docker, Kubernetes (k8s), Terraform and Postgres on Linux."
    )
    score, keywords = SCORER.score(job)
    assert score == 6
    assert set(keywords) == {"Python", "Docker", "Kubernetes", "Terraform", "PostgreSQL", "Linux"}


@pytest.mark.parametrize(
    "text, hit",
    [
        ("Experience with Go and gRPC", True),
        ("Our services are written in Go.", True),
        ("We use golang", True),
        ("Our go-to-market team", False),
        ("Go-to-market strategy", False),
        ("Go to market with partners", False),
        ("you will go deep on systems", False),  # lowercase "go" is the verb
        ("years ago", False),
    ],
)
def test_go_keyword_avoids_false_positives(text, hit):
    _, keywords = SCORER.score(make_job(description_text=text))
    assert ("Go" in keywords) is hit


def _match(score, days_ago):
    posted = None if days_ago is None else datetime(2026, 9, 24, tzinfo=UTC) - timedelta(days_ago)
    return Match(job=make_job(posted_at=posted), score=score, min_yoe=None, keywords=[])


def test_sort_by_score_then_recency_then_undated_last():
    a, b, c, d = _match(5, 3), _match(5, 1), _match(9, 10), _match(5, None)
    assert sort_matches([a, b, c, d]) == [c, b, a, d]
