"""Parse every shipped dashboard query with the real Prometheus parser."""
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    dashboard = json.loads((ROOT / 'deploy/grafana/rustybouncer-overview.json').read_text())
    expressions = [target['expr'] for panel in dashboard['panels'] for target in panel['targets']]
    if not expressions:
        raise ValueError('dashboard has no queries to validate')
    rules = []
    for index, expression in enumerate(expressions):
        expression = re.sub(r'\$\{(?:job|instance):regex\}', '.*', expression)
        if '$' in expression:
            raise ValueError(f'unhandled dashboard query variable: {expression}')
        rules.append({'record': f'rustybouncer_dashboard_validation_{index}', 'expr': expression})
    with tempfile.TemporaryDirectory(prefix='rustybouncer-dashboard-') as directory:
        path = Path(directory) / 'queries.json'
        path.write_text(json.dumps({'groups': [{'name': 'grafana-query-validation', 'rules': rules}]}))
        subprocess.run(['promtool', 'check', 'rules', str(path)], check=True)
    print(f'Grafana: {len(expressions)} PromQL targets validated.')


if __name__ == '__main__':
    main()
