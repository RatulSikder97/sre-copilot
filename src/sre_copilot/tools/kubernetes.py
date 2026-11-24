"""Read-only Kubernetes tools built on the official python client."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from kubernetes import client
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field, field_validator

from sre_copilot.kube import owned_replicasets, revision_of
from sre_copilot.safety import check_k8s_name, check_label_selector, check_namespace, redact
from sre_copilot.tools.base import Tool, ToolResult

# --------------------------------------------------------------------------------------------
# Argument models
# --------------------------------------------------------------------------------------------


class _NamespacedArgs(BaseModel):
    namespace: str = Field(description="Kubernetes namespace (must be allowlisted)")


class ListPodsArgs(_NamespacedArgs):
    label_selector: str | None = Field(default=None, description="e.g. 'app=checkout'")

    @field_validator("label_selector")
    @classmethod
    def _sel(cls, v: str | None) -> str | None:
        return None if v is None else check_label_selector(v)


class EventsArgs(_NamespacedArgs):
    involved_object: str | None = Field(
        default=None, description="Only events whose object name starts with this prefix"
    )
    limit: int = Field(default=25, ge=1, le=100)


class DescribeArgs(_NamespacedArgs):
    kind: Literal["pod", "deployment"]
    name: str

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_k8s_name(v)


class PodLogsArgs(_NamespacedArgs):
    pod: str | None = Field(default=None, description="Pod name; or pass `deployment` instead")
    deployment: str | None = Field(
        default=None, description="Pick the most-restarted pod of this deployment"
    )
    container: str | None = None
    previous: bool = Field(
        default=False, description="Logs of the previous (crashed) container instance"
    )
    tail_lines: int = Field(default=60, ge=1, le=500)

    @field_validator("pod", "deployment", "container")
    @classmethod
    def _names(cls, v: str | None) -> str | None:
        return None if v is None else check_k8s_name(v)


class RolloutHistoryArgs(_NamespacedArgs):
    deployment: str

    @field_validator("deployment")
    @classmethod
    def _dep(cls, v: str) -> str:
        return check_k8s_name(v, "deployment")


class RecentDeploysArgs(_NamespacedArgs):
    minutes: int = Field(default=120, ge=1, le=7 * 24 * 60)


# --------------------------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------------------------


def _ts(value: datetime | None) -> str | None:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _container_state(cs: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "name": cs.name,
        "ready": bool(cs.ready),
        "restarts": cs.restart_count or 0,
        "image": cs.image,
    }
    if cs.state and cs.state.waiting:
        state["waiting"] = {"reason": cs.state.waiting.reason, "message": cs.state.waiting.message}
    if cs.state and cs.state.running:
        state["running_since"] = _ts(cs.state.running.started_at)
    last = cs.last_state.terminated if cs.last_state else None
    if last:
        state["last_terminated"] = {
            "reason": last.reason,
            "exit_code": last.exit_code,
            "finished_at": _ts(last.finished_at),
        }
    return state


def _pod_summary(pod: Any) -> dict[str, Any]:
    statuses = pod.status.container_statuses or []
    return {
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "ready": f"{sum(1 for c in statuses if c.ready)}/{len(pod.spec.containers)}",
        "restarts": sum(c.restart_count or 0 for c in statuses),
        "created": _ts(pod.metadata.creation_timestamp),
        "template_hash": (pod.metadata.labels or {}).get("pod-template-hash"),
        "containers": [_container_state(c) for c in statuses],
    }


def _images(template: Any) -> list[str]:
    return [c.image for c in template.spec.containers]


def _env_summary(template: Any) -> dict[str, str]:
    env: dict[str, str] = {}
    for c in template.spec.containers:
        for e in c.env or []:
            if e.value_from is not None:
                env[e.name] = "<from secret/configmap ref>"
            else:
                env[e.name] = redact(f"{e.name}={e.value}").split("=", 1)[1]
    return env


class KubeReader:
    """Read-only Kubernetes access. The API client is created lazily on first use."""

    def __init__(
        self, api_factory: Callable[[], Any], allowed_namespaces: list[str], git_repo: Path | None
    ) -> None:
        self._api_factory = api_factory
        self.allowed = allowed_namespaces
        self.git_repo = git_repo

    @cached_property
    def _api(self) -> Any:
        return self._api_factory()

    @cached_property
    def core(self) -> Any:
        return client.CoreV1Api(self._api)

    @cached_property
    def apps(self) -> Any:
        return client.AppsV1Api(self._api)

    def _ns(self, namespace: str) -> str:
        return check_namespace(namespace, self.allowed)

    # -- tools ---------------------------------------------------------------------------

    def list_pods(self, args: ListPodsArgs) -> ToolResult:
        ns = self._ns(args.namespace)
        pods = self.core.list_namespaced_pod(ns, label_selector=args.label_selector or "").items
        rows = sorted((_pod_summary(p) for p in pods), key=lambda r: r["name"])
        unhealthy = [
            r["name"]
            for r in rows
            if r["phase"] != "Running" or any(not c["ready"] for c in r["containers"])
        ]
        waiting = sorted(
            {c["waiting"]["reason"] for r in rows for c in r["containers"] if "waiting" in c}
        )
        terminated = sorted(
            {
                f"{c['last_terminated']['reason']} (exit {c['last_terminated']['exit_code']})"
                for r in rows
                for c in r["containers"]
                if "last_terminated" in c
            }
        )
        summary = f"{len(rows)} pods in {ns}"
        if args.label_selector:
            summary += f" ({args.label_selector})"
        summary += f"; {len(unhealthy)} unhealthy"
        if waiting:
            summary += f"; waiting reasons: {', '.join(waiting)}"
        if terminated:
            summary += f"; last terminations: {', '.join(terminated)}"
        return ToolResult(output={"pods": rows, "unhealthy": unhealthy}, summary=summary)

    def events(self, args: EventsArgs) -> ToolResult:
        ns = self._ns(args.namespace)
        items = self.core.list_namespaced_event(ns).items
        rows = []
        for e in items:
            name = e.involved_object.name or ""
            if args.involved_object and not name.startswith(args.involved_object):
                continue
            last = e.last_timestamp or e.event_time or e.metadata.creation_timestamp
            rows.append(
                {
                    "last_seen": _ts(last),
                    "type": e.type,
                    "reason": e.reason,
                    "object": f"{(e.involved_object.kind or '').lower()}/{name}",
                    "count": e.count or 1,
                    "message": (e.message or "")[:300],
                }
            )
        rows.sort(key=lambda r: r["last_seen"] or "", reverse=True)
        rows = rows[: args.limit]
        warnings = [r for r in rows if r["type"] == "Warning"]
        reasons = sorted({r["reason"] for r in warnings})
        summary = f"{len(rows)} recent events in {ns}; {len(warnings)} warnings"
        if reasons:
            summary += f" ({', '.join(reasons)})"
        return ToolResult(output={"events": rows}, summary=summary)

    def describe(self, args: DescribeArgs) -> ToolResult:
        ns = self._ns(args.namespace)
        if args.kind == "deployment":
            d = self.apps.read_namespaced_deployment(args.name, ns)
            out = {
                "name": d.metadata.name,
                "revision": revision_of(d),
                "generation": d.metadata.generation,
                "replicas": {
                    "desired": d.spec.replicas,
                    "updated": d.status.updated_replicas or 0,
                    "ready": d.status.ready_replicas or 0,
                    "available": d.status.available_replicas or 0,
                    "unavailable": d.status.unavailable_replicas or 0,
                },
                "strategy": d.spec.strategy.type if d.spec.strategy else None,
                "images": _images(d.spec.template),
                "env": _env_summary(d.spec.template),
                "change_cause": (d.metadata.annotations or {}).get("kubernetes.io/change-cause"),
                "conditions": [
                    {
                        "type": c.type,
                        "status": c.status,
                        "reason": c.reason,
                        "message": (c.message or "")[:200],
                    }
                    for c in (d.status.conditions or [])
                ],
            }
            r = out["replicas"]
            summary = (
                f"deployment/{args.name} rev {out['revision']}: {r['available']}/{r['desired']} "
                f"available, {r['unavailable']} unavailable, images={out['images']}"
            )
            return ToolResult(output=out, summary=summary)

        p = self.core.read_namespaced_pod(args.name, ns)
        out = _pod_summary(p)
        out["node"] = p.spec.node_name
        out["resources"] = {
            c.name: {
                "limits": (c.resources.limits or {}) if c.resources else {},
                "requests": (c.resources.requests or {}) if c.resources else {},
            }
            for c in p.spec.containers
        }
        out["conditions"] = [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (p.status.conditions or [])
        ]
        summary = f"pod/{args.name}: phase={out['phase']} ready={out['ready']} "
        summary += f"restarts={out['restarts']}"
        return ToolResult(output=out, summary=summary)

    def _pick_pod(self, ns: str, deployment: str) -> str:
        d = self.apps.read_namespaced_deployment(deployment, ns)
        selector = ",".join(f"{k}={v}" for k, v in sorted(d.spec.selector.match_labels.items()))
        pods = self.core.list_namespaced_pod(ns, label_selector=selector).items
        if not pods:
            raise LookupError(f"deployment {deployment} has no pods")

        def rank(p: Any) -> tuple[int, str]:
            restarts = sum(c.restart_count or 0 for c in (p.status.container_statuses or []))
            return restarts, _ts(p.metadata.creation_timestamp) or ""

        name: str = max(pods, key=rank).metadata.name
        return name

    def pod_logs(self, args: PodLogsArgs) -> ToolResult:
        ns = self._ns(args.namespace)
        if not args.pod and not args.deployment:
            raise ValueError("pass either `pod` or `deployment`")
        pod = args.pod or self._pick_pod(ns, args.deployment or "")
        note = ""
        try:
            text = self.core.read_namespaced_pod_log(
                pod,
                ns,
                container=args.container,
                previous=args.previous,
                tail_lines=args.tail_lines,
            )
        except ApiException as exc:
            if not (args.previous and exc.status == 400):
                raise
            note = "no previous container instance; showing current logs"
            text = self.core.read_namespaced_pod_log(
                pod, ns, container=args.container, tail_lines=args.tail_lines
            )
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        out = {"pod": pod, "previous": args.previous, "lines": lines[-args.tail_lines :]}
        if note:
            out["note"] = note
        which = "previous" if args.previous and not note else "current"
        summary = f"{len(lines)} {which} log lines from pod/{pod}"
        if lines:
            summary += f"; last: {lines[-1][:160]}"
        return ToolResult(output=out, summary=summary)

    def rollout_history(self, args: RolloutHistoryArgs) -> ToolResult:
        ns = self._ns(args.namespace)
        d = self.apps.read_namespaced_deployment(args.deployment, ns)
        current = revision_of(d)
        rows = []
        for rs in owned_replicasets(self.apps, ns, args.deployment):
            ann = rs.metadata.annotations or {}
            rows.append(
                {
                    "revision": revision_of(rs),
                    "replicaset": rs.metadata.name,
                    "created": _ts(rs.metadata.creation_timestamp),
                    "images": _images(rs.spec.template),
                    "env": _env_summary(rs.spec.template),
                    "change_cause": ann.get("kubernetes.io/change-cause"),
                    "replicas": rs.status.replicas or 0,
                    "ready": rs.status.ready_replicas or 0,
                    "current": revision_of(rs) == current,
                }
            )
        summary = f"deployment/{args.deployment}: current revision {current}, {len(rows)} revisions"
        if rows:
            summary += f"; latest change-cause: {rows[0]['change_cause']!r} at {rows[0]['created']}"
        return ToolResult(output={"current_revision": current, "revisions": rows}, summary=summary)

    def recent_deploys(self, args: RecentDeploysArgs) -> ToolResult:
        ns = self._ns(args.namespace)
        since = datetime.now(UTC) - timedelta(minutes=args.minutes)
        deploys = []
        for rs in self.apps.list_namespaced_replica_set(ns).items:
            created = rs.metadata.creation_timestamp
            owner = next(
                (r.name for r in (rs.metadata.owner_references or []) if r.kind == "Deployment"),
                None,
            )
            if owner is None or created is None or created < since:
                continue
            deploys.append(
                {
                    "deployment": owner,
                    "revision": revision_of(rs),
                    "created": _ts(created),
                    "images": _images(rs.spec.template),
                    "change_cause": (rs.metadata.annotations or {}).get(
                        "kubernetes.io/change-cause"
                    ),
                }
            )
        deploys.sort(key=lambda r: r["created"] or "", reverse=True)
        out: dict[str, Any] = {"window_minutes": args.minutes, "kubernetes": deploys}
        if self.git_repo is not None:
            out["git"] = self._git_log(args.minutes)
        summary = f"{len(deploys)} rollouts in {ns} in the last {args.minutes}m"
        if deploys:
            d0 = deploys[0]
            summary += f"; newest: {d0['deployment']} rev {d0['revision']} at {d0['created']}"
        return ToolResult(output=out, summary=summary)

    def _git_log(self, minutes: int) -> list[dict[str, str]]:
        proc = subprocess.run(  # read-only: git log
            [
                "git",
                "-C",
                str(self.git_repo),
                "log",
                f"--since={minutes} minutes ago",
                "--pretty=format:%h|%an|%aI|%s",
                "-n",
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commits = []
        for line in proc.stdout.splitlines():
            sha, author, when, subject = [*line.split("|", 3), "", "", ""][:4]
            commits.append({"sha": sha, "author": author, "date": when, "subject": subject})
        return commits


def kubernetes_tools(reader: KubeReader) -> list[Tool]:
    return [
        Tool(
            "k8s_list_pods",
            "List pods with phase, readiness, restart counts, waiting reasons (e.g. "
            "CrashLoopBackOff) and last termination reason (e.g. OOMKilled).",
            ListPodsArgs,
            reader.list_pods,
        ),
        Tool(
            "k8s_events",
            "Recent Kubernetes events in a namespace, newest first (BackOff, Unhealthy, "
            "OOMKilling, FailedScheduling...).",
            EventsArgs,
            reader.events,
        ),
        Tool(
            "k8s_describe",
            "Describe a pod or deployment: replicas, conditions, images, env (redacted), "
            "resources and termination state.",
            DescribeArgs,
            reader.describe,
        ),
        Tool(
            "k8s_pod_logs",
            "Tail container logs for a pod (or the most-restarted pod of a deployment). Set "
            "previous=true to read the crashed instance of a restarting container.",
            PodLogsArgs,
            reader.pod_logs,
        ),
        Tool(
            "k8s_rollout_history",
            "Rollout history of a deployment: revisions, images, env, change-cause, readiness.",
            RolloutHistoryArgs,
            reader.rollout_history,
        ),
        Tool(
            "recent_deploys",
            "Rollouts across all deployments in a namespace within the window (and recent git "
            "commits when a repository is configured). Use to correlate onset with a change.",
            RecentDeploysArgs,
            reader.recent_deploys,
        ),
    ]
