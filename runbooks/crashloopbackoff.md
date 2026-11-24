# CrashLoopBackOff after a rollout

**Alert:** `KubePodCrashLooping` | **Owner:** service team on call | **Severity:** SEV2 if a rollout is stuck, SEV1 if no healthy replicas remain

## Symptoms
- Pods report `CrashLoopBackOff`; restart count climbs; `BackOff` warning events.
- A rolling update is stuck: the new ReplicaSet never becomes ready.

## Triage
1. `k8s_list_pods` for the workload: which pods are crashing, and which `pod-template-hash` (ReplicaSet) do they belong to? If only the newest ReplicaSet crashes, suspect the rollout.
2. `k8s_rollout_history` / `recent_deploys`: what changed (image, env, change-cause) and when? Compare with the alert start time.
3. `k8s_pod_logs` with `previous=true`: read the crashed instance's last lines. Exit code 1 with a config/validation error points at configuration; exit code 137 points at OOM (see `oomkilled.md`).
4. Check dependencies only if logs show connection errors; a process that exits during config validation never reached its dependencies.

## Remediation
- **Bad change (config or image):** roll back to the last healthy revision (`propose_rollback`). Rolling back is preferred over a restart: a restart re-creates pods from the same broken template.
- **Transient dependency failure:** fix the dependency; a `rollout restart` is appropriate once it is healthy.
- With `maxUnavailable: 0`, old pods keep serving during a stuck rollout; impact is reduced capacity and blocked deploys, not an outage. Still act quickly: an HPA scale-up or node drain would land new pods on the broken template.

## Verification
- Rollout completes (`updated == available == desired`), restart counts stop increasing, the alert resolves within two evaluation intervals.

## Follow-up
- Add schema validation for configuration in CI; block deploys whose config fails to parse.
