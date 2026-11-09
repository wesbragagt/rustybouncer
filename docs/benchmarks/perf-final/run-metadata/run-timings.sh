#!/usr/bin/env bash
set -euo pipefail
cd /home/publicuser/dev/rustybouncer/worktrees/scheduler
PERF_RESULTS=/home/publicuser/dev/rustybouncer-evidence/perf-scheduler-20261109
PERF_AFTER="$PERF_RESULTS/heads-frozen/production"
PERF_BEFORE=/home/publicuser/dev/rustybouncer-evidence/perf-pools-20261109/protocol-ownership-frozen/production
python3 benchmarks/pool_compare.py --output "$PERF_RESULTS/pools-after" --binary "$PERF_AFTER" > "$PERF_RESULTS/pools-after.log" 2>&1
python3 benchmarks/compare.py --output "$PERF_RESULTS/original-after" --binary "$PERF_AFTER" > "$PERF_RESULTS/original-after.log" 2>&1
python3 benchmarks/workload_compare.py --output "$PERF_RESULTS/workloads-after" --binary "$PERF_AFTER" > "$PERF_RESULTS/workloads-after.log" 2>&1
python3 benchmarks/pool_mixed.py --output "$PERF_RESULTS/pool-mixed" --before-binary "$PERF_BEFORE" --after-binary "$PERF_AFTER" > "$PERF_RESULTS/pool-mixed.log" 2>&1
