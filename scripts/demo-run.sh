#!/usr/bin/env bash
# End-to-end demo against the kind cluster:
#   inject fault -> Prometheus alert fires -> forwarded to the copilot webhook ->
#   investigation (replay or live model) with real read-only tools -> rollback proposal
#   (server-side dry run) -> human approval -> execution with the executor SA -> verification
#   -> audit-log verification.
#
# Usage: scripts/demo-run.sh crashloop|oom
# Env:   MODE=replay (default, no API key) | live (needs ANTHROPIC_API_KEY)
#        RECORD=1  record read-tool outputs + dry runs into the scenario's fixtures.json
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCENARIO="${1:?usage: demo-run.sh crashloop|oom}"
MODE="${MODE:-replay}"
PORT="${PORT:-8088}"
DEMO_DIR="$ROOT/.demo"
STATE="$DEMO_DIR/state-$SCENARIO"
SCENARIO_DIR="$ROOT/scenarios/checkout-$SCENARIO"
export KUBECONFIG="$DEMO_DIR/admin.kubeconfig"
cd "$ROOT"

case "$SCENARIO" in
  crashloop) ALERT=KubePodCrashLooping ;;
  oom) ALERT=KubeContainerOOMKilled ;;
  *) echo "unknown scenario $SCENARIO" >&2; exit 2 ;;
esac

rm -rf "$STATE"
export SRE_COPILOT_STATE_DIR="$STATE"
export SRE_COPILOT_KUBECONFIG_READ="$DEMO_DIR/reader.kubeconfig"
export SRE_COPILOT_KUBECONFIG_WRITE="$DEMO_DIR/executor.kubeconfig"
export SRE_COPILOT_PROMETHEUS_URL="http://127.0.0.1:39090"
export SRE_COPILOT_LOKI_URL="http://127.0.0.1:39100"
export SRE_COPILOT_ALLOWED_NAMESPACES="demo"
export SRE_COPILOT_VERIFY_TIMEOUT_SECONDS=240
SERVE_ARGS=()
if [[ "$MODE" == "replay" ]]; then
  SERVE_ARGS+=(--replay "$SCENARIO_DIR/script.yaml")
fi
if [[ "${RECORD:-0}" == "1" ]]; then
  SERVE_ARGS+=(--record "$SCENARIO_DIR/fixtures.json")
fi

echo "==> least privilege check"
can_i() { kubectl --kubeconfig "$1" auth can-i "${@:2}" 2>/dev/null || true; }
[[ "$(can_i "$SRE_COPILOT_KUBECONFIG_READ" patch deployments -n demo)" == "no" ]] \
  || { echo "reader SA must not patch deployments" >&2; exit 1; }
echo "reader SA cannot patch deployments (good)"
[[ "$(can_i "$SRE_COPILOT_KUBECONFIG_WRITE" patch deployments -n kube-system)" == "no" ]] \
  || { echo "executor SA must be namespace-scoped" >&2; exit 1; }
echo "executor SA cannot patch outside the allowlisted namespace (good)"
[[ "$(can_i "$SRE_COPILOT_KUBECONFIG_WRITE" delete pods -n demo)" == "no" ]] \
  || { echo "executor SA must not delete pods" >&2; exit 1; }
echo "executor SA cannot delete pods (good)"

echo "==> starting from a healthy deployment"
"$ROOT/scripts/fault.sh" reset

echo "==> waiting for earlier demo alerts to clear"
for _ in $(seq 1 60); do
  firing="$(curl -fsS -G "$SRE_COPILOT_PROMETHEUS_URL/api/v1/query" \
    --data-urlencode 'query=count(ALERTS{alertstate=~"pending|firing", namespace="demo"})' \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else 0)')"
  [[ "$firing" == "0" ]] && break
  sleep 5
done
[[ "$firing" == "0" ]] || { echo "demo alerts did not clear" >&2; exit 1; }

echo "==> injecting fault: $SCENARIO"
"$ROOT/scripts/fault.sh" "$SCENARIO"

echo "==> starting sre-copilot server ($MODE mode) on :$PORT"
uv run sre-copilot serve --port "$PORT" "${SERVE_ARGS[@]}" > "$DEMO_DIR/server-$SCENARIO.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done

echo "==> waiting for $ALERT and forwarding it as an Alertmanager webhook"
uv run sre-copilot fire-alert --alertname "$ALERT" \
  --webhook-url "http://127.0.0.1:$PORT" --out "$DEMO_DIR/report-$SCENARIO.md" --wait-firing 420

PROPOSAL="$(uv run sre-copilot proposals --status pending --json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); print(p[0]["id"] if p else "")')"
if [[ -z "$PROPOSAL" ]]; then
  echo "no pending proposal was created" >&2
  exit 1
fi

echo "==> human approval of $PROPOSAL (scripted for the demo; normally a person runs this)"
uv run sre-copilot approve "$PROPOSAL" --yes --actor demo-operator

echo "==> verifying recovery"
kubectl -n demo rollout status deploy/checkout --timeout=120s
kubectl -n demo get pods -l app=checkout
uv run sre-copilot audit verify
uv run sre-copilot audit show
echo "==> report written to $DEMO_DIR/report-$SCENARIO.md"
