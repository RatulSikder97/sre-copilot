"""Wire settings into concrete components (used by the CLI and the HTTP server)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sre_copilot.agent import Investigator
from sre_copilot.approval.executor import (
    Executor,
    FixtureExecutor,
    KubeExecutor,
    RecordingExecutor,
)
from sre_copilot.approval.service import ApprovalService
from sre_copilot.approval.store import AuditLog, Store
from sre_copilot.config import Settings
from sre_copilot.kube import api_client
from sre_copilot.llm.base import LLMClient
from sre_copilot.tools.base import FixtureStore
from sre_copilot.tools.toolbox import Toolbox


class ConfigurationError(ValueError):
    pass


@dataclass
class Runtime:
    settings: Settings
    store: Store
    audit: AuditLog
    approvals: ApprovalService
    toolbox: Toolbox
    fixtures: FixtureStore | None
    recorder: FixtureStore | None

    def investigator(self, llm: LLMClient | None = None) -> Investigator:
        return Investigator(
            self.settings, llm or build_llm(self.settings), self.toolbox, self.approvals, self.store
        )

    def save_recording(self) -> None:
        if self.recorder is not None and self.settings.record_path is not None:
            self.recorder.save(self.settings.record_path)


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm == "replay":
        if settings.replay_script is None:
            raise ConfigurationError("replay mode needs SRE_COPILOT_REPLAY_SCRIPT / --replay")
        from sre_copilot.llm.replay import ReplayLLM

        return ReplayLLM.from_path(settings.replay_script)
    from sre_copilot.llm.anthropic_client import AnthropicLLM

    return AnthropicLLM(settings)


def build_runtime(
    settings: Settings, executor_factory: Callable[[], Executor] | None = None
) -> Runtime:
    fixtures: FixtureStore | None = None
    if settings.tools == "fixtures":
        if settings.fixtures_path is None:
            raise ConfigurationError("fixtures mode needs SRE_COPILOT_FIXTURES_PATH / --fixtures")
        fixtures = FixtureStore.load(settings.fixtures_path)
    recorder = FixtureStore() if settings.record_path is not None else None

    if executor_factory is None:

        def default_executor() -> Executor:
            if fixtures is not None:
                return FixtureExecutor(fixtures)
            inner = KubeExecutor(
                api_client(settings.kubeconfig_write, settings.kube_context),
                verify_timeout=settings.verify_timeout_seconds,
            )
            return RecordingExecutor(inner, recorder) if recorder is not None else inner

        executor_factory = default_executor

    store = Store(settings.db_path)
    audit = AuditLog(settings.audit_log_path)
    approvals = ApprovalService(
        store,
        audit,
        executor_factory,
        allowed_namespaces=settings.allowed_namespaces,
        ttl_seconds=settings.approval_ttl_seconds,
        max_replicas=settings.max_scale_replicas,
        notify_webhook_url=settings.notify_webhook_url,
    )
    toolbox = Toolbox(settings, approvals, fixtures=fixtures, recorder=recorder)
    return Runtime(settings, store, audit, approvals, toolbox, fixtures, recorder)
