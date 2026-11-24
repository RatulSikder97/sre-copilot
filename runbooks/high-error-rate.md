# Elevated 5xx error rate

**Alert:** `HighErrorRate` | **Severity:** SEV2 above 2% for 5 minutes, SEV1 above 10%

## Triage
1. Scope: which routes, which pods, which version? Split error rate by `pod` and `version` labels with `prometheus_query_range`.
2. Correlate with `recent_deploys`. If errors started at a rollout and concentrate on new pods, treat it as a bad deploy.
3. Read error logs with `loki_query` (`contains: "error"`), look for a dominant exception.
4. Check saturation (CPU throttling, memory, connection pools) before blaming code.

## Remediation
- Bad deploy: `propose_rollback`.
- Saturation: `propose_scale` to add replicas, then find the bottleneck.
- Downstream dependency: fail over or shed load; restarts rarely help.
