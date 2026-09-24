from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class Company(BaseModel):
    name: str
    ats: str
    slug: str


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
    max_yoe: float = 3
    max_age_days: int = 14
    keep_undated: bool = True
    locations: LocationConfig
    scoring: ScoringConfig


def load_companies(path: Path = CONFIG_DIR / "companies.yaml") -> list[Company]:
    data = yaml.safe_load(path.read_text()) or {}
    return [Company(**c) for c in data.get("companies", [])]


def load_filters(path: Path = CONFIG_DIR / "filters.yaml") -> FilterConfig:
    return FilterConfig(**yaml.safe_load(path.read_text()))
