#!/usr/bin/env bash
set -euo pipefail
cd /home/publicuser/dev/rustybouncer/worktrees/scheduler
PERF_RESULTS=/home/publicuser/dev/rustybouncer-evidence/perf-scheduler-20261109
PERF_PRODUCTION="$PERF_RESULTS/heads-frozen/production"
PERF_PROFILE="$PERF_RESULTS/profile-after-frozen/rustybouncer"
# Launch only after all four matrices in run-timings.sh complete successfully.
# Current code already passed the full direct gate and normal checkpoint hook.
# Require their retained completion records; do not repeat unchanged verification.
test -f "$PERF_RESULTS/timing-matrices.passed"
test -f "$PERF_RESULTS/final-native.log"
python3 benchmarks/performance_profile.py --binary "$PERF_PRODUCTION" --profile-binary "$PERF_PROFILE" --output "$PERF_RESULTS/normalized-after" > "$PERF_RESULTS/normalized-after.log" 2>&1
python3 benchmarks/pool_profile.py --production-binary "$PERF_PRODUCTION" --profile-binary "$PERF_PROFILE" --output "$PERF_RESULTS/profiles-after" > "$PERF_RESULTS/profiles-after.log" 2>&1
python3 benchmarks/memory_audit.py --binary "$PERF_PROFILE" --output "$PERF_RESULTS/memory-after" > "$PERF_RESULTS/memory-after.log" 2>&1
python3 benchmarks/failover_load.py --binary "$PERF_PRODUCTION" --output "$PERF_RESULTS/failover-load" > "$PERF_RESULTS/failover-load.log" 2>&1
python3 -m tests.kind_cnpg --image rustybouncer:perf-scheduler-e4e9968 --output "$PERF_RESULTS/kind-after" > "$PERF_RESULTS/kind-after.log" 2>&1
python3 benchmarks/pool_profile_report.py --profiles "$PERF_RESULTS/profiles-after" --output "$PERF_RESULTS/profiles-after-summary.json" > "$PERF_RESULTS/profiles-after-analysis.log" 2>&1
python3 benchmarks/profile_report.py --baseline "$PERF_RESULTS/original-after" --normalized "$PERF_RESULTS/normalized-after" --profiles "$PERF_RESULTS/normalized-after" --output "$PERF_RESULTS/final-profile-summary.json" > "$PERF_RESULTS/final-profile-analysis.log" 2>&1
python3 benchmarks/final_report.py --run-root "$PERF_RESULTS" --pools-before /home/publicuser/dev/rustybouncer-evidence/perf-pools-20261109/pool-comparison --output "$PERF_RESULTS/final-report" > "$PERF_RESULTS/final-report.log" 2>&1
