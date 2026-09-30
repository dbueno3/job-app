from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from jobbot.models import Company

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class ConfigError(Exception):
    """A config file is invalid. The message says which entry and why."""


class LocationConfig(BaseModel):
    include: list[str]
    us_markers: list[str]
    non_us_markers: list[str]


class ScoringConfig(BaseModel):
    title_weight: int = 3
    description_weight: int = 1
    keywords: dict[str, str]


class FilterConfig(BaseModel):
    title_exclude: list[str]
    title_include: list[str]
    title_require: list[str]
    min_yoe: float = 0
    max_yoe: float = 3
    new_grad_patterns: list[str] = []
    max_age_days: int = 14
    keep_undated: bool = True
    locations: LocationConfig
    scoring: ScoringConfig


class Profile(BaseModel):
    skills: list[str] = []
    learning: list[str] = []
    cannot_claim: list[str] = []
    us_citizen: bool = False


def load_profile(path: Path = CONFIG_DIR / "profile.yaml") -> Profile:
    try:
        return Profile(**(yaml.safe_load(path.read_text()) or {}))
    except ValidationError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc


def load_companies(
    path: Path = CONFIG_DIR / "companies.yaml", include_disabled: bool = False
) -> list[Company]:
    """Load and validate companies.yaml. Raises ConfigError on any bad entry.

    Read-only: the file is never rewritten, so its comments (my notes) are preserved.
    """
    data = yaml.safe_load(path.read_text()) or {}
    companies = []
    for i, raw in enumerate(data.get("companies") or [], start=1):
        try:
            companies.append(Company(**raw))
        except (ValidationError, TypeError) as exc:
            label = raw.get("name", f"entry #{i}") if isinstance(raw, dict) else f"entry #{i}"
            raise ConfigError(f"{path.name}: invalid company {label!r}: {exc}") from exc
    dupes = [b for b, n in Counter((c.ats, c.slug) for c in companies).items() if n > 1]
    if dupes:
        listed = ", ".join(f"{ats}/{slug}" for ats, slug in dupes)
        raise ConfigError(f"{path.name}: duplicate (ats, slug): {listed}")
    return companies if include_disabled else [c for c in companies if c.enabled]


def load_filters(path: Path = CONFIG_DIR / "filters.yaml") -> FilterConfig:
    return FilterConfig(**yaml.safe_load(path.read_text()))
