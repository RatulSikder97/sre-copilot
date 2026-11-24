# Pods stuck in Pending

**Alert:** `KubePodNotScheduled` | **Severity:** SEV3 unless capacity is exhausted for a critical service

## Triage
1. `k8s_events` for `FailedScheduling`: insufficient CPU/memory, taints, node selectors, PVC binding.
2. `k8s_describe` the pod: requests vs. node allocatable.

## Remediation
- Lower over-sized requests through a normal change, add capacity, or scale down non-critical workloads.
- Do not delete pods to "unstick" them; they will be re-created Pending.
