#!/usr/bin/env bash
# Local lab on kind: LGTM-style observability + demo shop + Argus. Needs docker, kind, kubectl, helm.
set -euo pipefail
cd "$(dirname "$0")/.."

kind get clusters | grep -qx argus || kind create cluster --config deploy/kind/cluster.yaml

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null
helm repo update >/dev/null

helm upgrade --install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  -f deploy/helm/kube-prometheus-stack.yaml --wait --timeout 10m
helm upgrade --install loki grafana/loki -n monitoring -f deploy/helm/loki.yaml --wait --timeout 10m
helm upgrade --install tempo grafana/tempo -n monitoring -f deploy/helm/tempo.yaml --wait
helm upgrade --install otel open-telemetry/opentelemetry-collector -n monitoring -f deploy/helm/otel-collector.yaml --wait

docker build -t argus-demo/shop:1.3.2 demo/shop
docker build -t argus-demo/shop:1.4.0 --build-arg RELEASE_BUG=1 demo/shop
docker build -t argus-demo/argus:dev .
kind load docker-image argus-demo/shop:1.3.2 argus-demo/shop:1.4.0 argus-demo/argus:dev --name argus

kubectl apply -k gitops/
kubectl apply -f deploy/k8s/shop-monitoring.yaml -f deploy/k8s/argus.yaml
for d in frontend checkout payments; do kubectl -n shop rollout status "deploy/$d" --timeout=180s; done

cat <<MSG

Lab is up. Next:
  make break SCENARIO=bad-deploy      # or oom-kill, saturation
  make reports                         # Argus RCA reports (after the alert fires, ~3-5 min)
  kubectl -n monitoring port-forward svc/kps-grafana 3000:80   # Grafana, admin/admin
MSG
