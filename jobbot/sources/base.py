import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

import httpx

from jobbot.models import Job

log = logging.getLogger(__name__)

USER_AGENT = "jobbot/0.1 (personal job-search tool; sequential polite requests)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class BoardNotFound(Exception):
    """The board slug does not exist on this ATS (HTTP 404)."""


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


def get_json(
    client: httpx.Client,
    url: str,
    *,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """GET a JSON document, retrying transient failures with exponential backoff.

    404 raises BoardNotFound immediately (retrying won't help). 429/5xx and network
    errors are retried up to MAX_ATTEMPTS; other 4xx raise right away.
    """
    sleep = sleep or time.sleep
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.get(url)
            if resp.status_code == 404:
                raise BoardNotFound(url)
            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                raise _Retryable(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except (httpx.TransportError, _Retryable) as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            log.info("retrying %s in %.0fs (attempt %d): %s", url, delay, attempt, exc)
            sleep(delay)
    raise AssertionError("unreachable")


class _Retryable(Exception):
    pass


_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip tags, decode entities, and normalize whitespace (keeping line breaks)."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def mentions_remote(*locations: str) -> bool:
    return any("remote" in loc.lower() for loc in locations)


class Source(ABC):
    """One ATS adapter. Subclasses implement board_url() and parse()."""

    name: str

    @abstractmethod
    def board_url(self, slug: str) -> str: ...

    @abstractmethod
    def parse(self, payload: Any, company: str) -> list[Job]:
        """Convert a raw API payload into normalized Jobs. Pure: no I/O."""

    def fetch(self, client: httpx.Client, slug: str, company: str) -> list[Job]:
        return self.parse(get_json(client, self.board_url(slug)), company)
