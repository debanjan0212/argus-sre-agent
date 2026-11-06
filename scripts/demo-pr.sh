#!/usr/bin/env bash
# Show the GitOps flow end to end without a cluster: a throwaway Git repo gets the bad release,
# Argus investigates the recorded incident and commits the rollback on its own branch.
set -euo pipefail
cd "$(dirname "$0")/.."
repo=.demo/gitops
rm -rf "$repo" && mkdir -p .demo && cp -r gitops "$repo"
git -C "$repo" init -q -b main
git -C "$repo" -c user.name=dev -c user.email=dev@example.com add -A
git -C "$repo" -c user.name=dev -c user.email=dev@example.com commit -qm "baseline"
sed -i.bak 's#argus-demo/shop:1.3.2#argus-demo/shop:1.4.0#' "$repo/apps/checkout/deployment.yaml" && rm "$repo/apps/checkout/deployment.yaml.bak"
git -C "$repo" -c user.name=dev -c user.email=dev@example.com commit -qam "release checkout 1.4.0"
python -m argus investigate --scenario evals/scenarios/01-bad-deploy.yaml --gitops "$repo"
echo; git -C "$repo" log --oneline --all --graph
