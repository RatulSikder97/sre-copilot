"""Read-only Prometheus and Loki tools (HTTP APIs)."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field, field_validator

from sre_copilot.safety import check_k8s_name, check_namespace
from sre_copilot.tools.base import Tool, ToolResult

MAX_SERIES = 25


class PromQueryArgs(BaseModel):
    query: str = Field(description="PromQL expression", min_length=1, max_length=1000)


class PromRangeArgs(BaseModel):
    query: str = Field(description="PromQL expression", min_length=1, max_length=1000)
    minutes: int = Field(default=30, ge=1, le=24 * 60, description="Look-back window")
    step_seconds: int = Field(default=30, ge=5, le=3600)


class LokiArgs(BaseModel):
    namespace: str = Field(description="Kubernetes namespace (must be allowlisted)")
    app: str | None = Field(default=None, description="Value of the pod 'app' label")
    contains: str | None = Field(
        default=None, max_length=200, description="Case-sensitive substring line filter"
    )
    minutes: int = Field(default=30, ge=1, le=24 * 60)
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("app")
    @classmethod
    def _app(cls, v: str | None) -> str | None:
        return None if v is None else check_k8s_name(v, "app label")


def _labels(metric: dict[str, str]) -> str:
    name = metric.get("__name__", "")
    rest = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{rest}}}"


def _downsample(values: list[list[Any]], points: int = 12) -> list[list[Any]]:
    if len(values) <= points:
        return values
    stride = max(1, len(values) // points)
    sampled = values[::stride]
    if sampled[-1] is not values[-1]:
        sampled.append(values[-1])
    return sampled


class Prometheus:
    def __init__(self, base_url: str, public_url: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.public_url = (public_url or base_url).rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = httpx.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        payload: dict[str, Any] = resp.json() if resp.content else {}
        if resp.status_code >= 400 or payload.get("status") != "success":
            raise RuntimeError(f"prometheus {resp.status_code}: {payload.get('error', resp.text)}")
        data: dict[str, Any] = payload["data"]
        return data

    def link(self, query: str, minutes: int = 30) -> str:
        params = {"g0.expr": query, "g0.tab": "0", "g0.range_input": f"{minutes}m"}
        return f"{self.public_url}/graph?{urlencode(params)}"

    def query(self, args: PromQueryArgs) -> ToolResult:
        data = self._get("/api/v1/query", {"query": args.query})
        result = data.get("result", [])
        if data.get("resultType") == "scalar":
            series = [{"series": "scalar", "value": result[1]}]
        else:
            series = [{"series": _labels(r["metric"]), "value": r["value"][1]} for r in result]
        out = {"query": args.query, "series_count": len(series), "series": series[:MAX_SERIES]}
        summary = f"PromQL `{args.query}` -> {len(series)} series"
        if series:
            summary += f"; first={series[0]['series']} value={series[0]['value']}"
        return ToolResult(output=out, summary=summary, link=self.link(args.query))

    def query_range(self, args: PromRangeArgs) -> ToolResult:
        end = time.time()
        data = self._get(
            "/api/v1/query_range",
            {
                "query": args.query,
                "start": end - args.minutes * 60,
                "end": end,
                "step": args.step_seconds,
            },
        )
        series = []
        for r in data.get("result", [])[:MAX_SERIES]:
            values = r.get("values", [])
            nums = [float(v[1]) for v in values]
            series.append(
                {
                    "series": _labels(r["metric"]),
                    "min": min(nums) if nums else None,
                    "max": max(nums) if nums else None,
                    "last": nums[-1] if nums else None,
                    "samples": [
                        [time.strftime("%H:%M:%S", time.gmtime(float(t))), v]
                        for t, v in _downsample(values)
                    ],
                }
            )
        out = {"query": args.query, "window_minutes": args.minutes, "series": series}
        summary = f"PromQL range `{args.query}` over {args.minutes}m -> {len(series)} series"
        if series:
            s0 = series[0]
            summary += f"; {s0['series']} max={s0['max']} last={s0['last']}"
        return ToolResult(output=out, summary=summary, link=self.link(args.query, args.minutes))


class Loki:
    def __init__(
        self, base_url: str, public_url: str | None, timeout: float, allowed_namespaces: list[str]
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.public_url = (public_url or base_url).rstrip("/")
        self.timeout = timeout
        self.allowed = allowed_namespaces

    @staticmethod
    def build_query(args: LokiArgs) -> str:
        selector = f'namespace="{args.namespace}"'
        if args.app:
            selector += f', app="{args.app}"'
        query = "{" + selector + "}"
        if args.contains:
            escaped = args.contains.replace("\\", "\\\\").replace('"', '\\"')
            query += f' |= "{escaped}"'
        return query

    def query(self, args: LokiArgs) -> ToolResult:
        check_namespace(args.namespace, self.allowed)
        query = self.build_query(args)
        end_ns = time.time_ns()
        params: dict[str, str | int] = {
            "query": query,
            "start": end_ns - args.minutes * 60 * 1_000_000_000,
            "end": end_ns,
            "limit": args.limit,
            "direction": "backward",
        }
        resp = httpx.get(
            f"{self.base_url}/loki/api/v1/query_range", params=params, timeout=self.timeout
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"loki {resp.status_code}: {resp.text[:300]}")
        streams = resp.json().get("data", {}).get("result", [])
        lines: list[dict[str, str]] = []
        for stream in streams:
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                lines.append(
                    {
                        "ts": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts) / 1_000_000_000)
                        ),
                        "pod": labels.get("pod", ""),
                        "line": line[:500],
                    }
                )
        lines.sort(key=lambda entry: entry["ts"], reverse=True)
        lines = lines[: args.limit]
        out = {"query": query, "line_count": len(lines), "lines": lines}
        summary = f"LogQL `{query}` over {args.minutes}m -> {len(lines)} lines"
        if lines:
            summary += f"; latest: {lines[0]['line'][:160]}"
        link = f"{self.public_url}/loki/api/v1/query_range?{urlencode({'query': query})}"
        return ToolResult(output=out, summary=summary, link=link)


def observability_tools(prom: Prometheus, loki: Loki) -> list[Tool]:
    return [
        Tool(
            name="prometheus_query",
            description=(
                "Run an instant PromQL query against Prometheus. Use for current values, e.g. "
                "restart counts, waiting reasons from kube-state-metrics, firing ALERTS."
            ),
            input_model=PromQueryArgs,
            handler=prom.query,
        ),
        Tool(
            name="prometheus_query_range",
            description=(
                "Run a PromQL range query over the last N minutes. Returns min/max/last and a "
                "downsampled series per result; use it to see when a change started."
            ),
            input_model=PromRangeArgs,
            handler=prom.query_range,
        ),
        Tool(
            name="loki_query",
            description=(
                "Search container logs in Loki for an allowlisted namespace, optionally filtered "
                "by pod 'app' label and a substring. Newest lines first."
            ),
            input_model=LokiArgs,
            handler=loki.query,
        ),
    ]
