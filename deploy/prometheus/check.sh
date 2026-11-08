#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
promtool --version
for config in deploy/prometheus/prometheus.yml deploy/prometheus/prometheus.compose.yml deploy/prometheus/prometheus.cnpg.yml; do
  echo "+ promtool check config $config"
  promtool check config "$config"
done
echo '+ promtool check rules deploy/prometheus/rules.yml'
promtool check rules deploy/prometheus/rules.yml
echo '+ promtool test rules deploy/prometheus/rules.test.yml'
promtool test rules deploy/prometheus/rules.test.yml
