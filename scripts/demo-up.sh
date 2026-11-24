#!/usr/bin/env bash
# Create (or reuse) the `sre-copilot` kind cluster and deploy Prometheus, Loki,
# kube-state-metrics, promtail, the sample `checkout` app and the copilot RBAC.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="${CLUSTER:-sre-copilot}"
DEMO_DIR="$ROOT/.demo"
export KUBECONFIG="$DEMO_DIR/admin.kubeconfig"
mkdir -p "$DEMO_DIR"
cd "$ROOT"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "==> reusing kind cluster $CLUSTER"
  kind get kubeconfig --name "$CLUSTER" > "$KUBECONFIG"
else
  echo "==> creating kind cluster $CLUSTER"
  kind create cluster --name "$CLUSTER" --config deploy/kind/cluster.yaml \
    --kubeconfig "$KUBECONFIG" --wait 120s
fi

# Pre-load images from the host so the node does not depend on registry pulls from
# inside the kind container (slow/flaky behind some proxies and on Docker Desktop).
IMAGES=(
  registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.15.0
  prom/prometheus:v3.5.0
  grafana/loki:3.5.0
  grafana/promtail:3.5.0
  python:3.12-alpine
)
NODE="${CLUSTER}-control-plane"
case "$(docker exec "$NODE" uname -m)" in
  aarch64 | arm64) PLATFORM=linux/arm64 ;;
  *) PLATFORM=linux/amd64 ;;
esac
echo "==> pre-loading images ($PLATFORM)"
for image in "${IMAGES[@]}"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    for attempt in 1 2 3; do
      docker pull -q --platform "$PLATFORM" "$image" >/dev/null && break
      [[ $attempt == 3 ]] && { echo "failed to pull $image" >&2; exit 1; }
      sleep 5
    done
  fi
  # `kind load` works with the classic Docker image store; the single-platform `docker save`
  # path handles Docker Desktop's containerd store, where `kind load` can fail on multi-arch
  # images. If both fail the node pulls the image itself.
  if ! kind load docker-image --name "$CLUSTER" "$image" >/dev/null 2>&1; then
    docker save --platform "$PLATFORM" "$image" \
      | docker exec -i "$NODE" ctr -n k8s.io images import --digests --snapshotter=overlayfs - \
        >/dev/null 2>&1 \
      || echo "warning: could not preload $image; the node will pull it" >&2
  fi
done

echo "==> deploying observability stack, sample app and RBAC"
kubectl apply -f deploy/observability/
kubectl apply -f deploy/app/app.yaml
kubectl apply -f deploy/rbac/rbac.yaml

for d in kube-state-metrics prometheus loki; do
  kubectl -n monitoring rollout status "deploy/$d" --timeout=300s
done
kubectl -n monitoring rollout status ds/promtail --timeout=300s
kubectl -n demo rollout status deploy/checkout --timeout=300s

echo "==> minting least-privilege kubeconfigs"
"$ROOT/scripts/make-kubeconfig.sh" sre-copilot-reader > "$DEMO_DIR/reader.kubeconfig"
"$ROOT/scripts/make-kubeconfig.sh" sre-copilot-executor > "$DEMO_DIR/executor.kubeconfig"

echo "==> waiting for Prometheus (127.0.0.1:39090) and Loki (127.0.0.1:39100)"
for url in http://127.0.0.1:39090/-/ready http://127.0.0.1:39100/ready; do
  for _ in $(seq 1 90); do
    curl -fsS "$url" >/dev/null 2>&1 && break
    sleep 2
  done
  curl -fsS "$url" >/dev/null || { echo "not ready: $url" >&2; exit 1; }
done
echo "==> demo environment ready (KUBECONFIG=$KUBECONFIG)"
