#!/usr/bin/env bash
# Break the demo shop on purpose. Usage: scripts/break.sh bad-deploy|oom-kill|saturation|reset
set -euo pipefail
case "${1:-}" in
  bad-deploy) kubectl -n shop set image deploy/checkout checkout=argus-demo/shop:1.4.0 ;;
  oom-kill)   kubectl -n shop set env deploy/payments FAULT_LEAK_KB=256 ;;
  saturation) kubectl -n shop set env deploy/payments FAULT_CPU_MS=60
              kubectl -n shop scale deploy/loadgen --replicas=4 ;;
  reset)      kubectl apply -k "$(dirname "$0")/../gitops/"
              kubectl -n shop set env deploy/payments FAULT_LEAK_KB- FAULT_CPU_MS-
              kubectl -n shop scale deploy/loadgen --replicas=1 ;;
  *) echo "usage: $0 bad-deploy|oom-kill|saturation|reset" >&2; exit 1 ;;
esac
echo "done: ${1}. Alerts take a few minutes to fire; then run: make reports"
