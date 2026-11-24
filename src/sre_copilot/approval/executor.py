"""Write-path executors. Only ``ApprovalService`` calls ``execute``; the agent can only propose."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Protocol

from kubernetes import client
from kubernetes.client.rest import ApiException

from sre_copilot.kube import owned_replicasets, revision_of
from sre_copilot.models import ActionKind, ActionSpec, DryRunResult, ExecutionResult
from sre_copilot.tools.base import FixtureStore, canonical_args

RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"


class Executor(Protocol):
    def dry_run(self, action: ActionSpec) -> DryRunResult: ...

    def execute(self, action: ActionSpec, expected_generation: int | None) -> ExecutionResult: ...


def _template_view(template: dict[str, Any]) -> dict[str, Any]:
    containers = template.get("spec", {}).get("containers", [])
    return {
        "images": {c["name"]: c.get("image") for c in containers},
        "env": {
            f"{c['name']}.{e['name']}": e.get("value", "<ref>")
            for c in containers
            for e in c.get("env", []) or []
        },
        "annotations": dict(template.get("metadata", {}).get("annotations") or {}),
    }


def _diff(before: dict[str, Any], after: dict[str, Any], prefix: str = "") -> list[str]:
    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if isinstance(b, dict) and isinstance(a, dict):
            lines.extend(_diff(b, a, f"{prefix}{key}."))
        elif b != a:
            lines.append(f"{prefix}{key}: {b!r} -> {a!r}")
    return lines


class KubeExecutor:
    def __init__(self, api: Any, verify_timeout: float = 180.0, poll: float = 2.0) -> None:
        self.api = api
        self.apps = client.AppsV1Api(api)
        self.verify_timeout = verify_timeout
        self.poll = poll

    # -- helpers -------------------------------------------------------------------------

    def _template_dict(self, template: Any) -> dict[str, Any]:
        data: dict[str, Any] = self.api.sanitize_for_serialization(template)
        return data

    def _rollback_target(self, action: ActionSpec, dep: Any) -> tuple[int, dict[str, Any]]:
        current = revision_of(dep)
        replicasets = owned_replicasets(self.apps, action.namespace, action.deployment)
        if action.to_revision is not None:
            target = next((r for r in replicasets if revision_of(r) == action.to_revision), None)
        else:
            # "Previous" means the newest older revision that is still serving (has ready
            # replicas); if none is, fall back to the newest older revision. This avoids rolling
            # back onto an earlier broken revision when several bad rollouts are stacked.
            older = [r for r in replicasets if revision_of(r) < current]
            target = next((r for r in older if (r.status.ready_replicas or 0) > 0), None)
            target = target or (older[0] if older else None)
        if target is None:
            wanted = action.to_revision if action.to_revision is not None else "previous"
            raise LookupError(f"no ReplicaSet for revision {wanted} of {action.deployment}")
        template = self._template_dict(target.spec.template)
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        return revision_of(target), template

    def _patch(self, action: ActionSpec, dep: Any, dry_run: bool) -> tuple[Any, int | None]:
        kwargs: dict[str, Any] = {"dry_run": "All"} if dry_run else {}
        ns, name = action.namespace, action.deployment
        match action.kind:
            case ActionKind.ROLLOUT_RESTART:
                body = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {RESTARTED_AT: datetime.now(UTC).isoformat()}
                            }
                        }
                    }
                }
                return self.apps.patch_namespaced_deployment(name, ns, body, **kwargs), None
            case ActionKind.ROLLBACK:
                revision, template = self._rollback_target(action, dep)
                patch = [{"op": "replace", "path": "/spec/template", "value": template}]
                return self.apps.patch_namespaced_deployment(name, ns, patch, **kwargs), revision
            case ActionKind.SCALE:
                scale_body = {"spec": {"replicas": action.replicas}}
                self.apps.patch_namespaced_deployment_scale(name, ns, scale_body, **kwargs)
                if dry_run:
                    preview = self.apps.read_namespaced_deployment(name, ns)
                    preview.spec.replicas = action.replicas
                    return preview, None
                return self.apps.read_namespaced_deployment(name, ns), None

    def _change_cause(self, action: ActionSpec, revision: int) -> str | None:
        for rs in owned_replicasets(self.apps, action.namespace, action.deployment):
            if revision_of(rs) == revision:
                cause: str | None = (rs.metadata.annotations or {}).get(
                    "kubernetes.io/change-cause"
                )
                return cause
        return None

    # -- Executor API --------------------------------------------------------------------

    def dry_run(self, action: ActionSpec) -> DryRunResult:
        try:
            dep = self.apps.read_namespaced_deployment(action.deployment, action.namespace)
            before_tmpl = self._template_dict(dep.spec.template)
            preview, revision = self._patch(action, dep, dry_run=True)
        except ApiException as exc:
            return DryRunResult(ok=False, summary=f"server-side dry run failed: {exc.reason}")
        except LookupError as exc:
            return DryRunResult(ok=False, summary=str(exc))

        before = {"replicas": dep.spec.replicas, **_template_view(before_tmpl)}
        after = {
            "replicas": preview.spec.replicas,
            **_template_view(self._template_dict(preview.spec.template)),
        }
        diff = _diff(before, after)
        if action.kind is ActionKind.ROLLBACK and not any(
            not d.startswith("annotations.") for d in diff
        ):
            return DryRunResult(
                ok=False,
                summary=f"revision {revision} has the same pod template as the current one",
                resolved_revision=revision,
                observed_generation=dep.metadata.generation,
            )
        reviewed = action
        if revision is not None:
            reviewed = action.model_copy(update={"to_revision": revision})
        summary = f"server-side dry run OK: {reviewed.describe()}"
        if revision is not None:
            cause = self._change_cause(action, revision)
            if cause:
                summary += f" ({cause!r})"
        return DryRunResult(
            ok=True,
            summary=summary,
            diff=diff,
            resolved_revision=revision,
            observed_generation=dep.metadata.generation,
        )

    def execute(self, action: ActionSpec, expected_generation: int | None) -> ExecutionResult:
        try:
            dep = self.apps.read_namespaced_deployment(action.deployment, action.namespace)
            if expected_generation is not None and dep.metadata.generation != expected_generation:
                return ExecutionResult(
                    ok=False,
                    summary=(
                        "refused: deployment changed since the proposal was dry-run "
                        f"(generation {expected_generation} -> {dep.metadata.generation})"
                    ),
                )
            self._patch(action, dep, dry_run=False)
        except (ApiException, LookupError) as exc:
            reason = exc.reason if isinstance(exc, ApiException) else str(exc)
            return ExecutionResult(ok=False, summary=f"execution failed: {reason}")
        verified, detail = self.wait_for_rollout(action)
        return ExecutionResult(
            ok=True, summary=f"applied: {action.describe()}", verified=verified, verification=detail
        )

    def wait_for_rollout(self, action: ActionSpec) -> tuple[bool, str]:
        deadline = time.monotonic() + self.verify_timeout
        detail = "timed out"
        while time.monotonic() < deadline:
            d = self.apps.read_namespaced_deployment(action.deployment, action.namespace)
            want = d.spec.replicas or 0
            st = d.status
            updated, available = st.updated_replicas or 0, st.available_replicas or 0
            detail = (
                f"generation {st.observed_generation}/{d.metadata.generation}, "
                f"updated {updated}/{want}, available {available}/{want}, "
                f"unavailable {st.unavailable_replicas or 0}"
            )
            if (
                (st.observed_generation or 0) >= d.metadata.generation
                and updated == want
                and available == want
                and (st.replicas or 0) == want
                and not st.unavailable_replicas
            ):
                return True, f"rollout complete: {detail}"
            time.sleep(self.poll)
        return False, f"rollout not healthy within {self.verify_timeout:.0f}s: {detail}"


def action_key(action: ActionSpec) -> str:
    return canonical_args(action.model_dump(mode="json"))


class FixtureExecutor:
    """Replays recorded dry runs; refuses to execute anything."""

    def __init__(self, fixtures: FixtureStore) -> None:
        self.fixtures = fixtures

    def dry_run(self, action: ActionSpec) -> DryRunResult:
        recorded = self.fixtures.dry_runs.get(action_key(action))
        if recorded is None:
            return DryRunResult(ok=False, summary="no recorded dry run for this action (fixtures)")
        return DryRunResult.model_validate(recorded)

    def execute(self, action: ActionSpec, expected_generation: int | None) -> ExecutionResult:
        return ExecutionResult(ok=False, summary="fixtures mode: execution is disabled")


class RecordingExecutor:
    """Delegates to a real executor and records dry runs into a fixture store."""

    def __init__(self, inner: Executor, recorder: FixtureStore) -> None:
        self.inner = inner
        self.recorder = recorder

    def dry_run(self, action: ActionSpec) -> DryRunResult:
        result = self.inner.dry_run(action)
        self.recorder.dry_runs[action_key(action)] = result.model_dump(mode="json")
        return result

    def execute(self, action: ActionSpec, expected_generation: int | None) -> ExecutionResult:
        return self.inner.execute(action, expected_generation)
