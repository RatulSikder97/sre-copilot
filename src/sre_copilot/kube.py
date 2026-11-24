"""Kubernetes API client construction.

Read tools and the action executor use *separate* credentials: point
``SRE_COPILOT_KUBECONFIG_READ`` at a read-only service account and
``SRE_COPILOT_KUBECONFIG_WRITE`` at the narrowly-scoped executor account
(see ``deploy/rbac``). If unset, the default kubeconfig / in-cluster config is used.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kubernetes import client, config


def api_client(kubeconfig: Path | None, context: str | None = None) -> Any:
    if kubeconfig is not None:
        return config.new_client_from_config(config_file=str(kubeconfig), context=context)
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
        return client.ApiClient()
    return config.new_client_from_config(context=context)


def revision_of(obj: Any) -> int:
    annotations = obj.metadata.annotations or {}
    try:
        return int(annotations.get("deployment.kubernetes.io/revision", "0"))
    except ValueError:
        return 0


def owned_replicasets(apps: Any, namespace: str, deployment: str) -> list[Any]:
    """ReplicaSets owned by a deployment, newest revision first."""
    rs_list = apps.list_namespaced_replica_set(namespace).items
    owned = [
        rs
        for rs in rs_list
        if any(
            ref.kind == "Deployment" and ref.name == deployment
            for ref in (rs.metadata.owner_references or [])
        )
    ]
    return sorted(owned, key=revision_of, reverse=True)
