import os
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from urllib.parse import quote

from jobbot.models import utcnow
from jobbot.storage import StoredJob

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # implicit TLS
SMTP_TIMEOUT = 30


class DigestConfigError(Exception):
    pass


@dataclass
class SmtpConfig:
    user: str
    password: str
    to: str

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        """SMTP_USER, SMTP_APP_PASSWORD, and optional DIGEST_TO (defaults to SMTP_USER)."""
        user = os.environ.get("SMTP_USER", "").strip()
        password = os.environ.get("SMTP_APP_PASSWORD", "").replace(" ", "")
        if not user or not password:
            raise DigestConfigError(
                "SMTP_USER and SMTP_APP_PASSWORD must be set to send email "
                "(or use --dry-run). See https://myaccount.google.com/apppasswords"
            )
        to = os.environ.get("DIGEST_TO", "").strip() or user
        return cls(user=user, password=password, to=to)


@dataclass
class Digest:
    subject: str
    html: str
    text: str


def recruiter_search_url(company: str) -> str:
    return (
        "https://www.linkedin.com/search/results/people/"
        f"?keywords={quote(company)}%20technical%20recruiter"
    )


def format_yoe(min_yoe: float | None) -> str:
    return "not stated" if min_yoe is None else f"{min_yoe:g}+ yrs"


def build_digest(jobs: list[StoredJob], now: datetime | None = None) -> Digest:
    """Render new matches (already sorted by score, then recency) as HTML + plain text."""
    now = now or utcnow()
    n = len(jobs)
    subject = f"jobbot: {n} new SWE match{'es' if n != 1 else ''} ({now:%b %-d})"
    return Digest(subject=subject, html=_html(jobs, subject), text=_text(jobs))


def _html(jobs: list[StoredJob], subject: str) -> str:
    cell = "padding:8px 10px;border-bottom:1px solid #e5e5e5;vertical-align:top;"
    sub = "color:#666;font-size:12px;margin-top:2px;"
    head = f"{cell}text-align:left;background:#f5f5f5;font-weight:600;"
    rows = []
    for s in jobs:
        j = s.job
        posted = j.posted_at.strftime("%b %-d") if j.posted_at else "—"
        rows.append(
            "<tr>"
            f'<td style="{cell}text-align:right;color:#888;">{s.id}</td>'
            f'<td style="{cell}"><strong>{escape(j.company)}</strong></td>'
            f'<td style="{cell}"><a href="{escape(j.url)}">{escape(j.title)}</a>'
            f'<div style="{sub}">{escape(j.location)}</div></td>'
            f'<td style="{cell}text-align:center;">{s.score}</td>'
            f'<td style="{cell}white-space:nowrap;">{escape(format_yoe(s.min_yoe))}</td>'
            f'<td style="{cell}white-space:nowrap;">{posted}</td>'
            f'<td style="{cell}white-space:nowrap;">'
            f'<a href="{escape(j.url)}">Apply</a> · '
            f'<a href="{escape(recruiter_search_url(j.company))}">Recruiters</a></td>'
            "</tr>"
        )
    headers = "".join(
        f'<th style="{head}">{h}</th>'
        for h in ("#", "Company", "Role", "Score", "YOE", "Posted", "Links")
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:14px;color:#222;">'
        f'<h2 style="font-size:18px;margin:0 0 12px;">{escape(subject)}</h2>'
        '<table style="border-collapse:collapse;width:100%;">'
        f"<thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        '<p style="color:#888;font-size:12px;margin-top:16px;">'
        "Track one: <code>jobbot status &lt;#&gt; applied</code></p>"
        "</div>"
    )


def _text(jobs: list[StoredJob]) -> str:
    lines = []
    for s in jobs:
        j = s.job
        lines += [
            f"[{s.id}] {j.company} — {j.title}",
            f"     {j.location} | score {s.score} | YOE {format_yoe(s.min_yoe)}",
            f"     apply:      {j.url}",
            f"     recruiters: {recruiter_search_url(j.company)}",
            "",
        ]
    lines.append("Track one: jobbot status <#> applied")
    return "\n".join(lines)


def send_digest(digest: Digest, cfg: SmtpConfig) -> None:
    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = cfg.user
    msg["To"] = cfg.to
    msg.set_content(digest.text)
    msg.add_alternative(digest.html, subtype="html")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)
