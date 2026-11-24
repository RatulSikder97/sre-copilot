# Container OOMKilled

**Alert:** `KubeContainerOOMKilled` | **Severity:** SEV2 (SEV1 if all replicas are affected)

## Symptoms
- `last_terminated.reason = OOMKilled`, exit code 137, `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` is 1.
- Pods restart repeatedly and may enter `CrashLoopBackOff`.

## Triage
1. `k8s_list_pods`: confirm the termination reason is `OOMKilled` (not an application exit).
2. `k8s_describe` the pod: compare the container memory **limit** with normal usage.
3. `recent_deploys`: did a new revision start the OOMs? A step change right after a rollout means a regression (memory leak, new cache, larger buffers), not organic growth.
4. Logs (`k8s_pod_logs previous=true`, `loki_query`): look for allocations growing steadily before the kill (cache warm-up, batch loads).

## Remediation
- **Regression introduced by a rollout:** roll back (`propose_rollback`). Raising the limit only delays the next kill when memory grows without bound.
- **Organic growth / limit too tight:** raise the memory limit through the normal change process; consider scaling out (`propose_scale`) to spread load in the meantime.
- Never restart in a loop as a fix; it hides the leak and burns error budget.

## Verification
- No new OOMKilled terminations for 10 minutes; restart counters flat; rollout complete.

## Follow-up
- Add a memory-growth soak test to CI for the offending code path.
