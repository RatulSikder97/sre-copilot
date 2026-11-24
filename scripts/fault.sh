#!/usr/bin/env bash
# Fault injection for the demo `checkout` deployment.
#   crashloop - "deploy v2": a typo'd config value (DB_POOL_SIZE=2O) -> CrashLoopBackOff
#   oom       - "deploy v3": a cache warm-up that leaks memory -> OOMKilled
#   reset     - re-apply the healthy v1 manifest
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$ROOT/.demo/admin.kubeconfig}"

patch() {
  local cause="$1" env="$2"
  kubectl -n demo patch deployment checkout --type=strategic -p "$(cat <<JSON
{"metadata":{"annotations":{"kubernetes.io/change-cause":"${cause}"}},
 "spec":{"template":{"spec":{"containers":[{"name":"checkout","env":${env}}]}}}}
JSON
)"
}

case "${1:-}" in
  crashloop)
    patch "deploy v2: tune DB connection pool (DB_POOL_SIZE=2O)" \
      '[{"name":"APP_VERSION","value":"v2"},{"name":"DB_POOL_SIZE","value":"2O"}]'
    ;;
  oom)
    patch "deploy v3: enable response cache warm-up" \
      '[{"name":"APP_VERSION","value":"v3"},{"name":"DB_POOL_SIZE","value":"20"},{"name":"LEAK_MB_PER_SEC","value":"8"}]'
    ;;
  reset)
    kubectl apply -f "$ROOT/deploy/app/app.yaml"
    kubectl -n demo rollout status deploy/checkout --timeout=180s
    ;;
  *)
    echo "usage: $0 crashloop|oom|reset" >&2
    exit 2
    ;;
esac
