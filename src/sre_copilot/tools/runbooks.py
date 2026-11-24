"""Runbook search over local markdown files with Okapi BM25 (no external dependencies)."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from sre_copilot.tools.base import Tool, ToolResult

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "no",
        "not",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "why",
        "will",
        "with",
    }
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


@dataclass(frozen=True)
class Runbook:
    path: str
    title: str
    body: str


class BM25Index:
    def __init__(self, docs: list[Runbook], k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = docs
        self.k1 = k1
        self.b = b
        # Title tokens are counted twice: titles are short, high-signal summaries.
        self._tf = [Counter(tokenize(d.title) * 2 + tokenize(d.body)) for d in docs]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(docs)) if docs else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def from_dir(cls, directory: Path) -> BM25Index:
        docs = []
        for path in sorted(directory.glob("*.md")):
            text = path.read_text()
            title = next(
                (ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("# ")),
                path.stem,
            )
            docs.append(Runbook(path=str(path), title=title, body=text))
        return cls(docs)

    def search(self, query: str, k: int = 3) -> list[tuple[Runbook, float]]:
        terms = tokenize(query)
        scored: list[tuple[Runbook, float]] = []
        for doc, tf, length in zip(self.docs, self._tf, self._len, strict=True):
            score = 0.0
            for term in terms:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                norm = freq + self.k1 * (1 - self.b + self.b * length / (self._avg or 1))
                score += self._idf[term] * freq * (self.k1 + 1) / norm
            if score > 0:
                scored.append((doc, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]


class RunbookArgs(BaseModel):
    query: str = Field(min_length=2, max_length=300, description="Symptoms or keywords")
    k: int = Field(default=2, ge=1, le=5)


def runbook_tool(index: BM25Index, max_chars: int = 2500) -> Tool:
    def handler(args: RunbookArgs) -> ToolResult:
        hits = index.search(args.query, args.k)
        results = [
            {
                "path": doc.path,
                "title": doc.title,
                "score": round(score, 3),
                "content": doc.body[:max_chars],
            }
            for doc, score in hits
        ]
        summary = f"runbook search {args.query!r} -> " + (
            ", ".join(f"{r['title']} ({r['path']})" for r in results) or "no matches"
        )
        return ToolResult(output={"results": results}, summary=summary)

    return Tool(
        "runbook_search",
        "Search the team's markdown runbooks (BM25). Returns the best-matching runbooks with "
        "their content. Cite runbook paths in the report.",
        RunbookArgs,
        handler,
        fixture_exempt=True,
    )
