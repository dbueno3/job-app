from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class Company(BaseModel):
    name: str
    ats: str
    slug: str


def load_companies(path: Path = CONFIG_DIR / "companies.yaml") -> list[Company]:
    data = yaml.safe_load(path.read_text()) or {}
    return [Company(**c) for c in data.get("companies", [])]
