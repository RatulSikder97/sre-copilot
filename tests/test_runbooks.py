from __future__ import annotations

from pathlib import Path

from sre_copilot.tools.runbooks import BM25Index, tokenize

RUNBOOKS = Path(__file__).resolve().parents[1] / "runbooks"


def test_bm25_ranks_the_matching_runbook_first() -> None:
    index = BM25Index.from_dir(RUNBOOKS)
    assert index.search("container OOMKilled memory limit")[0][0].path.endswith("oomkilled.md")
    top = index.search("CrashLoopBackOff after rollout bad config")[0][0]
    assert top.path.endswith("crashloopbackoff.md")
    assert index.search("pods pending FailedScheduling")[0][0].path.endswith("pod-pending.md")


def test_no_match_returns_empty() -> None:
    assert BM25Index.from_dir(RUNBOOKS).search("zzzz qqqq") == []


def test_tokenize_drops_stopwords() -> None:
    assert tokenize("The pod IS crash-looping") == ["pod", "crash", "looping"]
