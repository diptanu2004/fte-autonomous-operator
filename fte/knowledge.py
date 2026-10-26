"""Company context: procedures (SOP retrieval), systems registry, and long-term lessons memory."""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("a an and are as at be by for from has have if in is it of on or so that the this to was were will with".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass
class Chunk:
    doc: str
    heading: str
    text: str


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.4, b: float = 0.75):
        self.toks = [tokenize(d) for d in docs]
        self.k1, self.b = k1, b
        self.avg = sum(map(len, self.toks)) / max(1, len(self.toks))
        df = Counter(t for doc in self.toks for t in set(doc))
        n = len(self.toks)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in self.toks]

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for tf, doc in zip(self.tf, self.toks):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(doc) / self.avg))
            out.append(s)
        return out


class CompanyContext:
    """Loads everything under company/: handbook SOPs, systems.yaml, permissions.yaml."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.docs: dict[str, str] = {
            p.stem: p.read_text(encoding="utf-8") for p in sorted((self.root / "handbook").glob("*.md"))
        }
        self.systems: dict[str, Any] = yaml.safe_load((self.root / "systems.yaml").read_text(encoding="utf-8"))
        self.permissions: dict[str, Any] = yaml.safe_load((self.root / "permissions.yaml").read_text(encoding="utf-8"))
        self.chunks: list[Chunk] = []
        for name, text in self.docs.items():
            title = text.splitlines()[0].lstrip("# ").strip() if text else name
            for part in re.split(r"\n(?=## )", text):
                heading = part.splitlines()[0].lstrip("# ").strip()
                self.chunks.append(Chunk(name, f"{title} › {heading}", part.strip()))
        self._index = BM25([f"{c.heading}\n{c.text}" for c in self.chunks])

    @property
    def company_name(self) -> str:
        return self.systems.get("company", {}).get("name", "the company")

    def doc_titles(self) -> str:
        return "\n".join(f"- {n}: {t.splitlines()[0].lstrip('# ')}" for n, t in self.docs.items())

    def search(self, query: str, k: int = 4) -> list[Chunk]:
        scored = sorted(zip(self._index.scores(query), self.chunks), key=lambda x: -x[0])
        return [c for s, c in scored[:k] if s > 0]

    def secret(self, name: str) -> str | None:
        env = os.environ.get(f"FTE_SECRET_{name.upper()}")
        return env if env is not None else (self.systems.get("secrets") or {}).get(name)

    def secret_names(self) -> list[str]:
        return list((self.systems.get("secrets") or {}).keys())

    def system(self, name: str) -> dict[str, Any] | None:
        return next((s for s in self.systems.get("systems", []) if s["name"] == name), None)

    def systems_text(self) -> str:
        lines = []
        for s in self.systems.get("systems", []):
            url = f" ({s['url']})" if s.get("url") else ""
            login = " — sign in with browser_login" if s.get("login") else ""
            lines.append(f"- {s['name']} [{s['kind']}]{url}: {s.get('description', '').strip()}{login}")
        return "\n".join(lines)


class LessonMemory:
    """Long-term memory: short, generalizable lessons distilled from past runs, retrieved by relevance."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # Governance comes only from company config (permissions.yaml), never from self-learned memory.
    _GOVERNANCE = re.compile(r"(?i)\b(approv\w*|permission\w*|polic\w*|bypass\w*|guardrail\w*|authori[sz]\w*|"
                             r"credential\w*|password\w*|sign-?off)\b")

    def accept(self, lesson: str, existing: list[str]) -> bool:
        lesson = lesson.strip()
        if len(lesson) < 15 or self._GOVERNANCE.search(lesson):
            return False
        new = set(tokenize(lesson))
        for old in existing:  # near-duplicate: most of the content words already in one lesson
            o = set(tokenize(old))
            if new and len(new & o) / len(new | o) >= 0.5:
                return False
        return True

    def add(self, lessons: list[str], task: str, run_id: str) -> list[str]:
        existing = [l["lesson"] for l in self.all()]
        kept = []
        with self.path.open("a", encoding="utf-8") as f:
            for lesson in lessons:
                if lesson and self.accept(lesson, existing + kept):
                    kept.append(lesson.strip())
                    f.write(json.dumps({"lesson": lesson.strip(), "task": task[:200], "run": run_id, "ts": time.time()}) + "\n")
        return kept

    def relevant(self, query: str, k: int = 5) -> list[str]:
        items = self.all()
        if not items:
            return []
        scores = BM25([f"{i['task']} {i['lesson']}" for i in items]).scores(query)
        ranked = sorted(zip(scores, items), key=lambda x: -x[0])
        return [i["lesson"] for s, i in ranked[:k] if s > 0]
