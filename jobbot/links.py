"""LinkedIn URLs generated for me to click. The tool never fetches any of these.

Job-search parameter codes (checked 2026-09 against published write-ups; LinkedIn
doesn't document them and treats them as hints):
  f_E      experience level: 1 internship, 2 entry level, 3 associate, 4 mid-senior, ...
  f_TPR    posted within: r<seconds>, e.g. r86400 = past 24 hours
  sortBy   DD = most recent first (R = relevance)
  f_WT     workplace type: 1 on-site, 2 remote, 3 hybrid. One source lists remote as 3
           under a different name (f_WRA); most, including a scraper library's own
           mapping, say f_WT=2 is remote.
"""

from urllib.parse import quote, urlencode

MIN_WINDOW_SECONDS = 3600  # LinkedIn's smallest useful window is about an hour
MAX_WINDOW_SECONDS = 30 * 24 * 3600


def recruiter_search_url(company: str) -> str:
    return (
        "https://www.linkedin.com/search/results/people/"
        f"?keywords={quote(company)}%20technical%20recruiter"
    )


def job_search_url(
    location: str,
    since_seconds: int,
    remote: bool = False,
    keywords: str = "software engineer",
) -> str:
    """Newest entry + associate SWE postings in `location` from the last `since_seconds`."""
    window = max(MIN_WINDOW_SECONDS, min(int(since_seconds), MAX_WINDOW_SECONDS))
    params = {
        "keywords": keywords,
        "location": location,
        "f_E": "2,3",
        "f_TPR": f"r{window}",
        "sortBy": "DD",
    }
    if remote:
        params["f_WT"] = "2"
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params, quote_via=quote)
