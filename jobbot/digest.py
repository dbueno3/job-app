import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape

from jobbot.links import recruiter_search_url
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


def format_yoe(min_yoe: float | None) -> str:
    return "not stated" if min_yoe is None else f"{min_yoe:g}+ yrs"


def format_age(s: StoredJob, now: datetime) -> str:
    """ "2h ago" from posted_at; "first seen 2h ago" when the ATS gave no date."""
    when = s.job.posted_at or s.job.first_seen_at
    minutes = max(0, int((now - when).total_seconds() // 60))
    if minutes < 2:
        ago = "just now"
    elif minutes < 60:
        ago = f"{minutes}m ago"
    elif minutes < 48 * 60:
        ago = f"{minutes // 60}h ago"
    else:
        ago = f"{minutes // (24 * 60)}d ago"
    return ago if s.job.posted_at else f"first seen {ago}"


_VARIANT_SPLIT = re.compile(r"\s+[-–—]\s+")


def split_title(title: str) -> tuple[str, str | None]:
    """("Software Engineer, Agent", "Retail") from "Software Engineer, Agent - Retail"."""
    parts = _VARIANT_SPLIT.split(title, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (title.strip(), None)


@dataclass
class Row:
    """One digest row: a single job, or same-company variants of one base title."""

    jobs: list[StoredJob]

    @property
    def lead(self) -> StoredJob:
        return self.jobs[0]

    @property
    def title(self) -> str:
        return split_title(self.lead.job.title)[0] if len(self.jobs) > 1 else self.lead.job.title

    @property
    def variants(self) -> list[tuple[str, StoredJob]]:
        return [(split_title(s.job.title)[1] or s.job.title, s) for s in self.jobs]

    @property
    def min_yoe(self) -> float | None:
        stated = [s.min_yoe for s in self.jobs if s.min_yoe is not None]
        return min(stated) if stated else None

    @property
    def score(self) -> int:
        return max(s.score for s in self.jobs)

    @property
    def keywords(self) -> list[str]:
        return list(dict.fromkeys(k for s in self.jobs for k in s.keywords))

    @property
    def gaps(self) -> list[str]:
        return list(dict.fromkeys(g for s in self.jobs for g in s.gaps))

    @property
    def compensation(self) -> str | None:
        return next((s.job.compensation for s in self.jobs if s.job.compensation), None)

    @property
    def ids(self) -> str:
        return ", ".join(str(s.id) for s in self.jobs)

    def facts(self, now: datetime) -> list[str]:
        parts = [self.lead.job.location, format_age(self.lead, now), f"score {self.score}"]
        parts.append(f"YOE {format_yoe(self.min_yoe)}")
        if self.compensation:
            parts.append(self.compensation)
        return parts


def group_rows(jobs: list[StoredJob]) -> list[Row]:
    """Group same-company roles sharing a base title; keeps the input (score) order."""
    rows: dict[tuple[str, str], Row] = {}
    for s in jobs:
        base, variant = split_title(s.job.title)
        key = (s.job.company.lower(), (base if variant else s.job.title).lower())
        if key in rows:
            rows[key].jobs.append(s)
        else:
            rows[key] = Row([s])
    return list(rows.values())


Section = tuple[str, list[StoredJob]]  # (heading, jobs sorted by score then age)
Footer = list[tuple[str, str]]  # (label, url)


def build_digest(
    sections: list[Section], now: datetime | None = None, footer: Footer | None = None
) -> Digest:
    """The regular digest: non-empty sections in order, then the LinkedIn footer."""
    now = now or utcnow()
    sections = [(h, jobs) for h, jobs in sections if jobs]
    n = sum(len(jobs) for _, jobs in sections)
    subject = f"jobbot: {n} new SWE match{'es' if n != 1 else ''} ({now:%b %-d})"
    return _render(subject, sections, now, footer or [])


def build_alert(jobs: list[StoredJob], now: datetime | None = None) -> Digest:
    """Immediate email for new hit-list (priority-1) matches."""
    now = now or utcnow()
    if len(jobs) == 1:
        subject = f"jobbot hit list: {jobs[0].job.company} — {jobs[0].job.title}"
    else:
        companies = ", ".join(dict.fromkeys(s.job.company for s in jobs))
        subject = f"jobbot hit list: {len(jobs)} new at {companies}"
    return _render(subject, [("Hit list", jobs)], now, [])


def _render(subject: str, sections: list[Section], now: datetime, footer: Footer) -> Digest:
    grouped = [(heading, group_rows(jobs)) for heading, jobs in sections]
    return Digest(subject, _html(subject, grouped, now, footer), _text(grouped, now, footer))


_FONT = "font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#222;"
_MUTED = "color:#666;font-size:12px;"


def _html_row(row: Row, now: datetime) -> str:
    j = row.lead.job
    cell = "padding:10px 8px;border-bottom:1px solid #e5e5e5;vertical-align:top;"
    title = f'<a href="{escape(j.url)}"><strong>{escape(row.title)}</strong></a>'
    lines = [f"<strong>{escape(j.company)}</strong> · {title}"]
    if len(row.jobs) > 1:
        links = " · ".join(
            f'<a href="{escape(v.job.url)}">{escape(name)}</a>' for name, v in row.variants
        )
        lines[0] += f' <span style="color:#666;">({len(row.jobs)} variants)</span>'
        lines.append(f'<div style="{_MUTED}">{links}</div>')
    lines.append(f'<div style="{_MUTED}">{escape(" · ".join(row.facts(now)))}</div>')
    if row.keywords:
        lines.append(
            f'<div style="{_MUTED}">keywords: <span style="color:#1a7f37;">'
            f"{escape(', '.join(row.keywords))}</span></div>"
        )
    if row.gaps:
        lines.append(
            f'<div style="{_MUTED}">gaps: <span style="color:#9a6700;">'
            f"{escape(', '.join(row.gaps))}</span></div>"
        )
    return (
        "<tr>"
        f'<td style="{cell}text-align:right;color:#888;width:1%;white-space:nowrap;">'
        f"{row.ids}</td>"
        f'<td style="{cell}">{"".join(lines)}</td>'
        f'<td style="{cell}white-space:nowrap;width:1%;">'
        f'<a href="{escape(j.url)}">Apply</a><br>'
        f'<a href="{escape(recruiter_search_url(j.company))}">Recruiters</a></td>'
        "</tr>"
    )


def _html(subject: str, grouped: list, now: datetime, footer: Footer) -> str:
    out = [
        f'<div style="{_FONT}">',
        f'<h2 style="font-size:18px;margin:0 0 8px;">{escape(subject)}</h2>',
    ]
    for heading, rows in grouped:
        out.append(f'<h3 style="font-size:15px;margin:18px 0 4px;">{escape(heading)}</h3>')
        out.append('<table style="border-collapse:collapse;width:100%;">')
        out.extend(_html_row(r, now) for r in rows)
        out.append("</table>")
    if footer:
        links = " · ".join(f'<a href="{escape(url)}">{escape(label)}</a>' for label, url in footer)
        out.append(
            f'<p style="{_MUTED}margin-top:18px;">Newest entry/associate SWE postings on '
            f"LinkedIn since the last digest: {links}</p>"
        )
    out.append(
        f'<p style="{_MUTED}margin-top:12px;">Track one: <code>jobbot status &lt;#&gt; '
        "applied</code>, then <code>jobbot sync</code></p></div>"
    )
    return "".join(out)


def _text(grouped: list, now: datetime, footer: Footer) -> str:
    lines = []
    for heading, rows in grouped:
        lines += [f"== {heading} ==", ""]
        for row in rows:
            j = row.lead.job
            count = f" ({len(row.jobs)} variants)" if len(row.jobs) > 1 else ""
            lines += [f"[{row.ids}] {j.company} — {row.title}{count}"]
            lines += [f"     {' | '.join(row.facts(now))}"]
            if row.keywords:
                lines.append(f"     keywords: {', '.join(row.keywords)}")
            if row.gaps:
                lines.append(f"     gaps: {', '.join(row.gaps)}")
            if len(row.jobs) > 1:
                lines += [f"     - {name}: {v.job.url}" for name, v in row.variants]
            else:
                lines.append(f"     apply:      {j.url}")
            lines += [f"     recruiters: {recruiter_search_url(j.company)}", ""]
    if footer:
        lines.append("LinkedIn, newest entry/associate SWE postings since the last digest:")
        lines += [f"  {label}: {url}" for label, url in footer]
        lines.append("")
    lines.append("Track one: jobbot status <#> applied, then jobbot sync")
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
