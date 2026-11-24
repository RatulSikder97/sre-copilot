"""Tool framework: typed tool specs, a registry, and fixture record/replay for read tools."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from sre_copilot.safety import redact, redact_obj

log = logging.getLogger(__name__)

ToolKind = Literal["read", "action"]


@dataclass
class ToolResult:
    output: Any
    summary: str
    link: str | None = None
    is_error: bool = False

    def render(self) -> str:
        if isinstance(self.output, str):
            return self.output
        return json.dumps(self.output, indent=1, default=str, sort_keys=True)

    def to_json(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "summary": self.summary,
            "link": self.link,
            "is_error": self.is_error,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ToolResult:
        return cls(
            output=data["output"],
            summary=data["summary"],
            link=data.get("link"),
            is_error=bool(data.get("is_error", False)),
        )


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any], ToolResult]
    kind: ToolKind = "read"
    # Local, side-effect-free tools (runbook search) run for real even in fixtures mode.
    fixture_exempt: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": strip_titles(self.input_model.model_json_schema()),
        }


def strip_titles(schema: Any) -> Any:
    """Remove pydantic's auto-generated ``title`` keys to keep tool schemas compact."""
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key in ("properties", "$defs"):
                # Keys here are field / definition names (a field may be called "title").
                out[key] = {name: strip_titles(sub) for name, sub in value.items()}
            elif key != "title":
                out[key] = strip_titles(value)
        return out
    if isinstance(schema, list):
        return [strip_titles(v) for v in schema]
    return schema


def canonical_args(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


class FixtureMissingError(KeyError):
    pass


@dataclass
class FixtureStore:
    """Recorded tool results keyed by ``(tool, canonical args)``."""

    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    dry_runs: dict[str, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def key(name: str, args: dict[str, Any]) -> str:
        return f"{name} {canonical_args(args)}"

    @classmethod
    def load(cls, path: Path) -> FixtureStore:
        data = json.loads(path.read_text())
        return cls(
            tools={cls.key(e["tool"], e["args"]): e["result"] for e in data.get("tools", [])},
            dry_runs={e["key"]: e["result"] for e in data.get("dry_runs", [])},
        )

    def lookup(self, name: str, args: dict[str, Any]) -> ToolResult:
        key = self.key(name, args)
        if key not in self.tools:
            raise FixtureMissingError(f"no recorded fixture for {key}")
        return ToolResult.from_json(self.tools[key])

    def save(self, path: Path) -> None:
        tools = []
        for key, result in self.tools.items():
            name, _, args = key.partition(" ")
            tools.append({"tool": name, "args": json.loads(args), "result": result})
        dry_runs = [{"key": k, "result": v} for k, v in self.dry_runs.items()]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"tools": tools, "dry_runs": dry_runs}, indent=1, default=str) + "\n"
        )


class ToolRegistry:
    """Dispatches tool calls; validates input; optionally replays or records fixtures."""

    def __init__(
        self,
        tools: list[Tool],
        *,
        fixtures: FixtureStore | None = None,
        recorder: FixtureStore | None = None,
    ) -> None:
        self._tools = {t.name: t for t in tools}
        self.fixtures = fixtures
        self.recorder = recorder

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def invoke(self, name: str, raw_args: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                output=f"unknown tool {name!r}", summary="unknown tool", is_error=True
            )
        try:
            args = tool.input_model.model_validate(raw_args)
        except ValidationError as exc:
            return ToolResult(
                output=f"invalid arguments: {exc.errors(include_url=False)}",
                summary=f"{name}: invalid arguments",
                is_error=True,
            )
        normalized = args.model_dump(mode="json")

        if self.fixtures is not None and tool.kind == "read" and not tool.fixture_exempt:
            try:
                replayed = self.fixtures.lookup(name, normalized)
                replayed.output = redact_obj(replayed.output)
                return replayed
            except FixtureMissingError as exc:
                return ToolResult(output=str(exc), summary=f"{name}: no fixture", is_error=True)

        try:
            result = tool.handler(args)
        except PermissionError as exc:
            result = ToolResult(output=f"denied: {exc}", summary=f"{name}: denied", is_error=True)
        except Exception as exc:  # tool failures are reported to the model, never raised
            log.warning("tool %s failed: %s", name, exc)
            result = ToolResult(
                output=f"{type(exc).__name__}: {redact(str(exc))}",
                summary=f"{name}: {type(exc).__name__}",
                is_error=True,
            )
        result.output = redact_obj(result.output)

        if self.recorder is not None and tool.kind == "read" and not tool.fixture_exempt:
            self.recorder.tools[FixtureStore.key(name, normalized)] = result.to_json()
        return result
