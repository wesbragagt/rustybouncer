#!/usr/bin/env python3
"""Summarize complete PERF-001 evidence; never filter failed or slow arms."""
import argparse
import json
import math
import re
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.compare import TARGETS  # noqa: E402


def load(path):
    return json.loads(path.read_text())


def spread(values):
    center = statistics.mean(values)
    return {'all': values, 'median': statistics.median(values), 'min': min(values),
            'max': max(values), 'sample_cv': statistics.stdev(values) / center if center else 0}


def enrich(rows, root):
    enriched=[]
    for row in rows:
        item=dict(row)
        directory=root/f'c{row["clients"]}-r{row["repetition"]}-{row["target"]}'
        boundaries=directory/'measurement-resource-boundaries.json'
        if boundaries.exists():
            snapshots=load(boundaries)
            before={thread['tid']:thread for thread in snapshots['before']['threads']}
            after={thread['tid']:thread for thread in snapshots['after']['threads']}
            for key in ('voluntary_ctxt_switches','nonvoluntary_ctxt_switches'):
                delta=sum(thread[key]-before.get(tid,{key:0})[key] for tid,thread in after.items())
                assert delta>=0
                item[f'{key}_per_q']=delta/row['transactions']
            item['threads_absent_at_measurement_end']=sorted(set(before)-set(after))
        enriched.append(item)
    return enriched


def matrix(rows):
    expected = {(clients, repetition, target) for clients in (10, 100)
                for repetition in (1, 2, 3) for target in TARGETS}
    actual = [(r['clients'], r['repetition'], r['target']) for r in rows]
    assert len(rows) == 30 and set(actual) == expected, 'incomplete or duplicate timing matrix'
    assert all(r['errors'] == 0 and r['transactions'] > 0 for r in rows)
    result = []
    for clients in (10, 100):
        for target in TARGETS:
            group = [r for r in rows if r['clients'] == clients and r['target'] == target]
            fields = ('tps', 'p50_ms', 'p95_ms', 'p99_ms', 'backend_connections_peak',
                      'cpu_seconds_per_q_including_connect', 'rss_peak_bytes_observed',
                      'voluntary_ctxt_switches_per_q','nonvoluntary_ctxt_switches_per_q')
            result.append({'clients': clients, 'target': target,
                           **{key: spread([r[key] for r in group]) for key in fields
                              if all(key in r for r in group)}})
    return result


def switches(path, started, ended):
    """Matched per-thread switch intervals; includes sleep AND runnable delay."""
    pattern = re.compile(r'(\d+)\s+([\d.]+): PERF_RECORD_SWITCH (OUT|IN)\b')
    pending, durations, unmatched_in = {}, [], 0
    with path.open() as source:
        for line in source:
            found = pattern.search(line)
            if not found:
                continue
            tid, timestamp, direction = int(found[1]), float(found[2]), found[3]
            if direction == 'OUT':
                pending[tid] = timestamp
            elif tid in pending:
                begin = pending.pop(tid)
                duration = max(0, min(ended, timestamp) - max(started, begin))
                if duration:
                    durations.append(duration)
            else:
                unmatched_in += 1
    assert durations, 'no matched in-window switch intervals'
    return {'matched_in_window': len(durations), 'aggregate_thread_offcpu_seconds': sum(durations),
            'longest_interval_seconds': max(durations), 'unmatched_in': unmatched_in,
            'unmatched_out': len(pending), 'wall_seconds': ended - started,
            'interpretation': 'sum over threads; sleep plus runnable delay, not exact kernel blocking stacks; incomplete edge intervals excluded'}


def category(stack):
    # Exclusive observed-callchain buckets. Missing/inlined callers remain
    # unresolved; these shares are not estimates of hypothetical isolated costs.
    rules = (
        ('classification', ('router::classify', 'router::scan_words', 'router::cte::')),
        ('metrics', ('rustybouncer::metrics::',)),
        ('topology_discovery', ('rustybouncer::discover', 'rustybouncer::probe')),
        ('frame_read', ('router::message',)),
        ('frame_write', ('router::send',)),
        ('result_accumulation', ('router::result',)),
        ('routing_and_dispatch', ('router::route_query', 'router::execute_query', 'router::wait_route')),
        ('response_delivery', ('router::deliver_query',)),
        ('passthrough_copy', ('copy_bidirectional', 'copy_buf',)),
        ('runtime_or_other', ()),
    )
    for name, needles in rules:
        if not needles or any(needle in stack for needle in needles):
            return name
    raise AssertionError('unreachable')


def sample_categories(path):
    counts, weights, self_weights = Counter(), Counter(), Counter()
    frames, weight = [], 0
    def complete():
        if not weight:
            return
        bucket=category('\n'.join(frames))
        counts[bucket]+=1
        weights[bucket]+=weight
        if frames:
            self_weights[frames[0]]+=weight
        else:
            self_weights['unresolved']+=weight
    with path.open() as source:
        for line in source:
            if 'cpu-clock:u:' in line or 'PERF_RECORD_SWITCH ' in line:
                complete()
                frames,weight=[],0
                if 'cpu-clock:u:' in line:
                    period=re.search(r'(\d+)\s+cpu-clock:u:',line)
                    assert period, 'sampling period absent; cannot silently assume equal weights'
                    weight=int(period[1])
            elif weight and line.strip():
                symbol=re.sub(r'^[0-9a-f]+\s+', '',line.strip())
                frames.append(symbol)
    complete()
    total=sum(weights.values())
    assert total>0 and sum(counts.values())>0, 'no CPU samples'
    return {'samples':sum(counts.values()), 'period_weight':total,
            'ranked_categories':[{'category':name,'samples':counts[name],'period_share':value/total}
                                 for name,value in weights.most_common()],
            'ranked_self_symbols':[{'symbol':name,'period_share':value/total}
                                   for name,value in self_weights.most_common(40)],
            'interpretation':'exclusive observed user callchain buckets in documented precedence; unresolved/inlined frames and kernel execution are not attributed; not isolated marginal costs'}


def heap_summary(path, queries):
    pairs = [tuple(map(int,line.split())) for line in path.read_text().splitlines()]
    assert pairs and all(len(pair)==2 and min(pair)>=0 for pair in pairs)
    calls = sum(count for _,count in pairs)
    allocated = sum(size*count for size,count in pairs)
    assert calls>0 and allocated>0 and queries>0
    return {'allocation_calls':calls, 'allocated_bytes_including_reallocations':allocated,
            'calls_per_controlled_q':calls/queries,'allocated_bytes_per_controlled_q':allocated/queries,
            'scope':'whole lifetime including startup/background/preflight; controlled-Q amortization, not isolated steady state'}


def ensure_histograms(root):
    for directory in sorted((root/'heap').iterdir()):
        if not directory.is_dir():
            continue
        histogram=directory/'allocation-sizes.tsv'
        if histogram.exists():
            continue
        files=list(directory.glob('heap*.gz'))+list(directory.glob('heap*.zst'))
        assert len(files)==1
        command=['heaptrack_print','-f',str(files[0]),'-n','30','-s','5','-H',str(histogram)]
        result=subprocess.run(command,text=True,capture_output=True,timeout=120,check=True)
        (directory/'histogram-command.json').write_text(json.dumps(command)+'\n')
        (directory/'allocations-with-histogram.txt').write_text(result.stdout+result.stderr)


def profiles(root):
    rows = load(root / 'profiles.json')
    expected = {(mode, clients, target) for mode in ('cpu', 'offcpu', 'heap', 'syscalls')
                for clients in (10, 100) for target in TARGETS if target.startswith('rustybouncer')}
    assert len(rows) == 16 and {(r['mode'], r['clients'], r['target']) for r in rows} == expected
    result = []
    for row in rows:
        assert row['errors'] == 0 and row['queries'] > 0
        directory = root / row['mode'] / f'c{row["clients"]}-{row["target"]}'
        item = dict(row)
        if row['mode'] == 'syscalls':
            item['syscalls_per_controlled_q'] = {
                name: {**counts, 'calls_per_q': counts['calls'] / row['queries'],
                       'bytes_per_q': counts['returned_bytes'] / row['queries']}
                for name, counts in load(directory / 'syscalls.json').items()}
        if row['mode'] == 'cpu':
            item['sampled_cpu'] = sample_categories(directory/'stacks.txt')
        if row['mode'] == 'heap':
            item['heap'] = heap_summary(directory/'allocation-sizes.tsv',row['queries'])
        if row['mode'] == 'offcpu':
            item['switch_intervals'] = switches(directory / 'stacks.txt',
                                                row['measurement_started_monotonic'],
                                                row['measurement_finished_monotonic'])
        result.append(item)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--normalized', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    args = parser.parse_args()
    ensure_histograms(args.profiles)
    unrestricted = matrix(load(args.baseline / 'results.json'))
    equal_core = matrix(enrich(load(args.normalized / 'normalized.json'),args.normalized))
    result = {'unrestricted': unrestricted,
              'equal_core': equal_core,
              'profiles': profiles(args.profiles),
              'uncertainty': 'Three repetitions on a shared host. Ranges/CV are descriptive, not confidence in production SLOs; profiles are separate from timing.'}
    assert all(math.isfinite(row['tps']['median']) for row in unrestricted + equal_core)
    args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
