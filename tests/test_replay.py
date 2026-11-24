"""Replay backend: expectations catch divergence between script and environment."""

from __future__ import annotations

import pytest

from sre_copilot.llm.replay import ReplayDivergenceError, ReplayExhaustedError
from tests.helpers import replay

TOOLS = [{"name": "k8s_list_pods"}, {"name": "submit_report"}]


def test_divergence_detected_on_next_turn() -> None:
    llm = replay(
        [
            {
                "tool_calls": [
                    {"name": "k8s_list_pods", "input": {}, "expect": ["CrashLoopBackOff"]}
                ]
            },
            {"text": "next"},
        ]
    )
    turn = llm.complete(system="", messages=[], tools=TOOLS)
    call_id = turn.tool_calls[0].id
    messages = [
        {"role": "assistant", "content": turn.content},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "all Running"}],
        },
    ]
    with pytest.raises(ReplayDivergenceError, match="CrashLoopBackOff"):
        llm.complete(system="", messages=messages, tools=TOOLS)


def test_unknown_tool_and_exhaustion() -> None:
    llm = replay([{"tool_calls": [{"name": "propose_scale", "input": {}}]}])
    with pytest.raises(ReplayDivergenceError, match="not offered"):
        llm.complete(system="", messages=[], tools=TOOLS)
    with pytest.raises(ReplayExhaustedError):
        replay([]).complete(system="", messages=[], tools=TOOLS)


def test_proposal_reference_requires_a_recorded_proposal() -> None:
    llm = replay([{"tool_calls": [{"name": "submit_report", "input": {"p": "{{proposal:0}}"}}]}])
    with pytest.raises(ReplayDivergenceError, match="proposal 0"):
        llm.complete(system="", messages=[], tools=TOOLS)
