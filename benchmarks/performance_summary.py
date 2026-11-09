#!/usr/bin/env python3
"""Verify and summarize complete pool/workload matrices without dropping any run."""
import argparse
import itertools
import json
import math
import statistics
from pathlib import Path

POOL_TARGETS=('pgbouncer-session','pgbouncer-transaction','rustybouncer-pool-session','rustybouncer-pool-transaction')
LOOKUP_TARGETS=('direct','pgbouncer-session','pgbouncer-transaction','rustybouncer-session','rustybouncer-routed')
PLACEMENTS=('unrestricted','normalized')


def identity(row):
    return (row['placement'],row['workload'],row['clients'],row['target'],row['repetition'])


def contract(kind):
    if kind=='pools':
        return POOL_TARGETS,('churn','transactions'),(10,100,1000)
    assert kind=='workloads'
    return LOOKUP_TARGETS,('cte','large','mixed'),(10,100)


def validate(rows,kind):
    targets,workloads,clients=contract(kind)
    expected=set(itertools.product(PLACEMENTS,workloads,clients,targets,(1,2,3)))
    actual=[identity(row) for row in rows]
    assert len(actual)==len(set(actual)),'duplicate matrix arm'
    assert set(actual)==expected,{'missing':sorted(expected-set(actual)),'unexpected':sorted(set(actual)-expected)}
    for row in rows:
        assert row['errors']==0,row
        assert row['transactions']>=row['clients'],row
        assert 0<row['backend_connections_peak'],row
        if kind=='pools':
            assert row['backend_connections_peak']<=row['pool_size']==16,row
            assert row['sample_count']==row['transactions'],row
            assert row['clients_sampled']==row['clients'],row
            assert row['client_completions_min']>=1,row
        else:
            assert row['latencies']['all']['samples']==row['transactions'],row
            assert row['latencies']['clients_completed']==row['clients'],row
            assert row['latencies']['client_completions_min']>=1,row


def stats(values):
    assert values and all(math.isfinite(value) and value>=0 for value in values),values
    mean=statistics.mean(values)
    return {'all':values,'median':statistics.median(values),'minimum':min(values),'maximum':max(values),
            'sample_cv_percent':statistics.stdev(values)/mean*100 if len(values)>1 and mean else 0}


def measurements(row,kind):
    quantiles=row if kind=='pools' else row['latencies']['all']
    result={'tps':row['tps_including_connect_and_drain'] if kind=='pools' else row['tps'],
            'p50_ms':quantiles['p50_ms'],'p95_ms':quantiles['p95_ms'],'p99_ms':quantiles['p99_ms'],
            'backend_connections_peak':row['backend_connections_peak'],
            'wall_seconds':row['wall_seconds']}
    if kind=='pools':
        result['maximum_latency_ms']=row['maximum_latency_ms']
        result['client_completions_min']=row['client_completions_min']
    if row['target']!='direct':
        result['cpu_seconds_per_transaction']=row['cpu_seconds_per_transaction'] if kind=='pools' else row['cpu_seconds_per_q_including_connect']
        result['rss_peak_bytes_observed']=row['rss_peak_bytes_observed']
        result['rss_lifetime_hwm_bytes']=row['rss_lifetime_hwm_bytes']
    if row['workload']=='mixed':
        result.update({'short_p99_ms':row['latencies']['scripts']['0']['p99_ms'],
                       'long_p99_ms':row['latencies']['scripts']['1']['p99_ms']})
    return result


def grouped(rows,kind):
    groups={}
    for row in sorted(rows,key=identity):
        key=identity(row)[:-1]
        groups.setdefault(key,[]).append(row)
    result=[]
    for (placement,workload,clients,target),runs in sorted(groups.items()):
        assert [row['repetition'] for row in runs]==[1,2,3]
        measured=[measurements(row,kind) for row in runs]
        names=set(measured[0])
        assert all(set(row)==names for row in measured)
        result.append({'placement':placement,'workload':workload,'clients':clients,'target':target,
                       'repetitions':[1,2,3],'metrics':{name:stats([row[name] for row in measured]) for name in sorted(names)}})
    return result


def paired(rows,kind):
    indexed={identity(row):row for row in rows}
    _,workloads,clients=contract(kind)
    pairs=(('rustybouncer-pool-session','pgbouncer-session'),('rustybouncer-pool-transaction','pgbouncer-transaction')) if kind=='pools' else (
        ('rustybouncer-session','pgbouncer-session'),('rustybouncer-routed','pgbouncer-session'))
    result=[]
    for placement,workload,count,(native,reference) in itertools.product(PLACEMENTS,workloads,clients,pairs):
        ratios={name:[] for name in ('tps_ratio','p95_ratio','p99_ratio')}
        for repetition in (1,2,3):
            first=measurements(indexed[(placement,workload,count,native,repetition)],kind)
            second=measurements(indexed[(placement,workload,count,reference,repetition)],kind)
            for output,name in (('tps_ratio','tps'),('p95_ratio','p95_ms'),('p99_ratio','p99_ms')):
                assert second[name]>0
                ratios[output].append(first[name]/second[name])
        result.append({'placement':placement,'workload':workload,'clients':count,'native':native,'reference':reference,
                       'paired_repetitions':[1,2,3],'ratios':{name:stats(values) for name,values in ratios.items()}})
    return result


def report(rows,kind):
    validate(rows,kind)
    return {'kind':kind,'required_arms':len(rows),'errors':sum(row['errors'] for row in rows),
            'repetitions':3,'all_repetitions_retained':True,'groups':grouped(rows,kind),'paired':paired(rows,kind),
            'interpretation':'Medians/ranges/CV of three complete repetitions; paired ratios use matching repetition numbers. Shared-host observations are not randomized causal estimates, statistical significance or production SLOs. Quantile medians do not pool raw samples across repetitions. Direct has no proxy CPU/RSS.'}


def markdown(summary):
    lines=['# '+('Bounded pools' if summary['kind']=='pools' else 'Additional workloads'),'',summary['interpretation'],'',
           '| Placement | Workload | Clients | Target | Median TPS | TPS min–max | TPS CV % | p95 ms | p99 ms | Peak backends |',
           '| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for group in summary['groups']:
        metrics=group['metrics']
        tps=metrics['tps']
        lines.append(f'| {group["placement"]} | {group["workload"]} | {group["clients"]} | {group["target"]} | '
                     f'{tps["median"]:,.1f} | {tps["minimum"]:,.1f}–{tps["maximum"]:,.1f} | {tps["sample_cv_percent"]:.2f} | '
                     f'{metrics["p95_ms"]["median"]:.3f} | {metrics["p99_ms"]["median"]:.3f} | {metrics["backend_connections_peak"]["maximum"]:g} |')
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind',choices=('pools','workloads'),required=True)
    parser.add_argument('--results',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    rows=json.loads(args.results.read_text())
    summary=report(rows,args.kind)
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    (args.output/'summary.md').write_text(markdown(summary))


if __name__=='__main__':
    main()
