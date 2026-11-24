#!/usr/bin/env bash
# Print a kubeconfig for a service account in the `sre-copilot` namespace,
# using a short-lived token from the TokenRequest API.
set -euo pipefail
SA="$1"
DURATION="${TOKEN_DURATION:-4h}"
server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
ca="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
token="$(kubectl -n sre-copilot create token "$SA" --duration="$DURATION")"
cat <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
  - name: sre-copilot
    cluster:
      server: ${server}
      certificate-authority-data: ${ca}
users:
  - name: ${SA}
    user:
      token: ${token}
contexts:
  - name: ${SA}
    context:
      cluster: sre-copilot
      user: ${SA}
      namespace: demo
current-context: ${SA}
KUBECONFIG
