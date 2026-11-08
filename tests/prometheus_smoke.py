"""D04/S10: ingest real router metrics and evaluate shipped rules in Prometheus.

Requires Docker, the release router, promtool and PROMETHEUS_BIN (server binary).
All resources are owned; HTTP listeners bind only to loopback.
"""
import contextlib
import json
import math
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import failover as f
from metrics_support import MetricsCluster, free_port


def api(port, endpoint, **query):
    url = f'http://127.0.0.1:{port}/api/v1/{endpoint}'
    if query:
        url += '?' + urllib.parse.urlencode(query)
    with urllib.request.urlopen(url, timeout=3) as response:
        payload = json.load(response)
    assert payload['status'] == 'success', payload
    return payload['data']


def value(port, expression):
    result = api(port, 'query', query=expression)['result']
    if len(result) != 1:
        return None
    number = float(result[0]['value'][1])
    return number if math.isfinite(number) else None


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main():
    output = Path(os.environ['PROMETHEUS_EVIDENCE']).resolve()
    output.mkdir(parents=True, exist_ok=False)
    server = os.environ['PROMETHEUS_BIN']
    cluster = MetricsCluster(output / 'fixture')
    process = None
    started = time.monotonic()
    try:
        cluster.start()
        port = free_port()
        # Profile intentionally matches neither example: freshness-profile alerts
        # are outside this short ingestion test and are covered by rule fixtures.
        config = {'global': {'scrape_interval': '1s', 'evaluation_interval': '1s'},
                  'rule_files': [str(f.ROOT / 'deploy/prometheus/rules.yml')],
                  'scrape_configs': [{'job_name': 'rustybouncer',
                                     'static_configs': [{'targets': [f'127.0.0.1:{cluster.metrics_port}']}]}]}
        config_path = output / 'prometheus.json'
        config_path.write_text(json.dumps(config, indent=2))
        command = [server, '--config.file=' + str(config_path), '--storage.tsdb.path=' + str(output / 'data'),
                   f'--web.listen-address=127.0.0.1:{port}']
        (output / 'command.json').write_text(json.dumps(command))
        with (output / 'prometheus.log').open('w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        f.eventually(lambda: value(port, 'up{job="rustybouncer"}') == 1, description='real Prometheus scrape up')
        expression = 'sum(rustybouncer_queries_total{kind="read",outcome="ok"})'
        baseline = value(port, expression)
        assert baseline is not None
        with contextlib.closing(cluster.connect()) as client:
            for _ in range(50):
                assert client.query('SELECT 42') == [['42']]
            f.eventually(lambda: value(port, expression) == baseline + 50,
                         description='50 Q messages ingested without duplication')
            rates = ['rustybouncer:q_messages_per_second:rate5m',
                     'rustybouncer:q_duration_p95_seconds:rate5m']
            for expression in rates:
                f.eventually(lambda expression=expression: (value(port, expression) or 0) > 0,
                             description=f'recording rule {expression}')
            assert value(port, 'sum(rustybouncer_node_role{role="primary"})') == 1
            assert value(port, 'sum(rustybouncer_topology_nodes{role="replica"})') == 2
        cluster.idle()
        report = {'passed': True, 'q_messages': 50, 'baseline_read_messages': baseline,
                  'seconds': time.monotonic() - started, 'targets': api(port, 'targets'),
                  'rules': api(port, 'rules')}
        (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({k: report[k] for k in ('passed', 'q_messages', 'seconds')}))
    finally:
        try:
            if process is not None:
                stop(process)
        finally:
            cluster.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
