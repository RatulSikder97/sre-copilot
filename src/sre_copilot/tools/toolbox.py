"""Tool assembly for an investigation."""

from __future__ import annotations

from sre_copilot.approval.service import ApprovalService
from sre_copilot.config import Settings
from sre_copilot.kube import api_client
from sre_copilot.tools.actions import action_tools
from sre_copilot.tools.base import FixtureStore, Tool, ToolRegistry
from sre_copilot.tools.kubernetes import KubeReader, kubernetes_tools
from sre_copilot.tools.observability import Loki, Prometheus, observability_tools
from sre_copilot.tools.runbooks import BM25Index, runbook_tool


class Toolbox:
    """Long-lived tool backends; hands out a per-investigation registry."""

    def __init__(
        self,
        settings: Settings,
        approvals: ApprovalService,
        *,
        fixtures: FixtureStore | None = None,
        recorder: FixtureStore | None = None,
    ) -> None:
        self.settings = settings
        self.approvals = approvals
        self.fixtures = fixtures
        self.recorder = recorder
        self.prometheus = Prometheus(
            settings.prometheus_url, settings.prometheus_public_url, settings.http_timeout_seconds
        )
        self.loki = Loki(
            settings.loki_url,
            settings.loki_public_url,
            settings.http_timeout_seconds,
            settings.allowed_namespaces,
        )
        self.kube = KubeReader(
            lambda: api_client(settings.kubeconfig_read, settings.kube_context),
            settings.allowed_namespaces,
            settings.git_repo,
        )
        self.runbooks = BM25Index.from_dir(settings.runbooks_dir)

    def read_tools(self) -> list[Tool]:
        return [
            *observability_tools(self.prometheus, self.loki),
            *kubernetes_tools(self.kube),
            runbook_tool(self.runbooks),
        ]

    def registry(self, investigation_id: str) -> ToolRegistry:
        return ToolRegistry(
            [*self.read_tools(), *action_tools(self.approvals, investigation_id)],
            fixtures=self.fixtures,
            recorder=self.recorder,
        )
