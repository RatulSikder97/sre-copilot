"""Read tools against mocked HTTP APIs, and registry behaviour."""

from __future__ import annotations

import httpx
import respx

from sre_copilot.tools.base import FixtureStore, Tool, ToolRegistry, ToolResult
from sre_copilot.tools.observability import (
    Loki,
    LokiArgs,
    Prometheus,
    PromQueryArgs,
    PromRangeArgs,
    observability_tools,
)

PROM = "http://prom.test"
LOKI = "http://loki.test"


@respx.mock
def test_prometheus_instant_query() -> None:
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {"__name__": "up", "pod": "a"}, "value": [1, "1"]}],
                },
            },
        )
    )
    result = Prometheus(PROM, "http://public", 5).query(PromQueryArgs(query="up"))
    assert result.output["series"] == [{"series": 'up{pod="a"}', "value": "1"}]
    assert result.link is not None
    assert result.link.startswith("http://public/graph?g0.expr=up")


@respx.mock
def test_prometheus_range_summarises_series() -> None:
    values = [[1_700_000_000 + i * 30, str(i)] for i in range(40)]
    respx.get(f"{PROM}/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [{"metric": {"pod": "a"}, "values": values}],
                },
            },
        )
    )
    result = Prometheus(PROM, None, 5).query_range(PromRangeArgs(query="x", minutes=20))
    series = result.output["series"][0]
    assert (series["min"], series["max"], series["last"]) == (0.0, 39.0, 39.0)
    assert len(series["samples"]) <= 14


@respx.mock
def test_prometheus_error_is_raised() -> None:
    respx.get(f"{PROM}/api/v1/query").mock(
        return_value=httpx.Response(400, json={"status": "error", "error": "parse error"})
    )
    registry = ToolRegistry(
        observability_tools(Prometheus(PROM, None, 5), Loki(LOKI, None, 5, ["demo"]))
    )
    result = registry.invoke("prometheus_query", {"query": "up{"})
    assert result.is_error
    assert "parse error" in result.output


@respx.mock
def test_loki_query_builds_safe_logql_and_redacts() -> None:
    route = respx.get(f"{LOKI}/loki/api/v1/query_range").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "result": [
                        {
                            "stream": {"pod": "checkout-1"},
                            "values": [
                                ["1700000000000000000", "connecting to postgres://u:hunter2@db/x"]
                            ],
                        }
                    ]
                }
            },
        )
    )
    loki = Loki(LOKI, None, 5, ["demo"])
    registry = ToolRegistry(observability_tools(Prometheus(PROM, None, 5), loki))
    result = registry.invoke(
        "loki_query", {"namespace": "demo", "app": "checkout", "contains": 'say "hi"'}
    )
    assert not result.is_error
    assert (
        route.calls[0].request.url.params["query"]
        == '{namespace="demo", app="checkout"} |= "say \\"hi\\""'
    )
    assert "hunter2" not in result.render()


def test_loki_namespace_allowlist() -> None:
    registry = ToolRegistry(
        observability_tools(Prometheus(PROM, None, 5), Loki(LOKI, None, 5, ["demo"]))
    )
    result = registry.invoke("loki_query", {"namespace": "kube-system"})
    assert result.is_error
    assert "allowlist" in result.output


def test_loki_query_rejects_label_injection() -> None:
    assert LokiArgs.model_fields["app"] is not None
    registry = ToolRegistry(
        observability_tools(Prometheus(PROM, None, 5), Loki(LOKI, None, 5, ["demo"]))
    )
    result = registry.invoke("loki_query", {"namespace": "demo", "app": 'x"} or {a=~".+'})
    assert result.is_error


def test_registry_validation_unknown_tool_and_recording() -> None:
    calls: list[str] = []

    def handler(args: PromQueryArgs) -> ToolResult:
        calls.append(args.query)
        return ToolResult(output={"token": "abc", "q": args.query}, summary="ok")

    recorder = FixtureStore()
    registry = ToolRegistry([Tool("t", "d", PromQueryArgs, handler)], recorder=recorder)
    assert registry.invoke("nope", {}).is_error
    assert registry.invoke("t", {"query": ""}).is_error  # min_length=1
    ok = registry.invoke("t", {"query": "up"})
    assert ok.output == {"token": "[REDACTED]", "q": "up"}
    assert calls == ["up"]
    replayed = ToolRegistry([Tool("t", "d", PromQueryArgs, handler)], fixtures=recorder)
    assert replayed.invoke("t", {"query": "up"}).output == ok.output
    assert calls == ["up"]  # served from fixtures, handler not called
    missing = replayed.invoke("t", {"query": "other"})
    assert missing.is_error
    assert "no recorded fixture" in missing.output
