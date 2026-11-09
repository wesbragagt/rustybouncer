#!/usr/bin/env python3
"""PERF-005/009: assemble complete, provenance-bound final evidence, without filters.

Requires every planned matrix. Instrumented attribution and native timing remain
separate. This report does not establish broader canonical release conformance.
"""
import argparse
import hashlib
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from benchmarks import performance_summary as s  # noqa: E402
from benchmarks.memory_audit import TARGETS as MEMORY_TARGETS  # noqa: E402
from benchmarks.profile_report import matrix  # noqa: E402


def read(path):
    return json.loads(path.read_text())


def exact(rows,expected,key):
    expected=set(expected)
    actual=[key(row) for row in rows]
    assert len(actual)==len(set(actual)),'duplicate arm'
    assert set(actual)==expected,{'missing':sorted(expected-set(actual)),
                                 'unexpected':sorted(set(actual)-expected)}


def delta(before,after,kind):
    """All three matching repetition ratios; no pooled percentile or significance."""
    previous={s.identity(row):row for row in before}
    current={s.identity(row):row for row in after}
    assert set(previous)==set(current)
    groups={}
    for key in sorted(previous):
        old,new=previous[key],current[key]
        if not new['target'].startswith('rustybouncer'):
            continue
        measured_old=s.measurements(old,kind)
        measured_new=s.measurements(new,kind)
        common=set(measured_old)&set(measured_new)
        ratios={name:measured_new[name]/measured_old[name] for name in common if measured_old[name]>0}
        groups.setdefault(key[:-1],[]).append({'repetition':key[-1],'ratios':ratios})
    result=[]
    for (placement,workload,clients,target),runs in groups.items():
        assert [row['repetition'] for row in runs]==[1,2,3]
        names=set(runs[0]['ratios'])
        assert all(set(row['ratios'])==names for row in runs)
        result.append({'placement':placement,'workload':workload,'clients':clients,'target':target,
            'ratios':{name:s.stats([row['ratios'][name] for row in runs]) for name in sorted(names)}})
    return result


def mixed(rows):
    expected=itertools.product(('before','after'),s.PLACEMENTS,(10,100),s.POOL_TARGETS,(1,2,3))
    exact(rows,expected,lambda row:(row['candidate'],row['placement'],row['clients'],row['target'],row['repetition']))
    for row in rows:
        assert row['workload']=='mixed' and row['errors']==0
        assert 0<row['backend_connections_peak']<=row['pool_size']==16
        latency=row['latencies']
        assert latency['all']['samples']==row['transactions']>=row['clients']
        assert latency['clients_completed']==row['clients'] and latency['client_completions_min']>=1
        assert set(latency['scripts'])=={'0','1'}
    before=[row for row in rows if row['candidate']=='before']
    after=[row for row in rows if row['candidate']=='after']
    return {'arms':96,'errors':0,'before':s.grouped(before,'workloads'),
            'after':s.grouped(after,'workloads'),'after_over_before':delta(before,after,'workloads')}


def faults(root):
    rows=read(root/'results.json')
    exact(rows,itertools.product((10,100),s.POOL_TARGETS,(1,2,3)),
          lambda row:(row['clients'],row['target'],row['repetition']))
    for row in rows:
        count=row['clients']
        assert row['active_workers']==row['recovered_workers']==count
        assert row['unexpected_operation_errors']==row['replayed_operations']==0
        assert row['duplicate_backend_insert_executions']==0
        assert row['logged_backend_insert_executions']>=row['actual_committed_writes']
        assert row['acknowledged_writes']>=count*2 and row['stable_operations']>=count
        assert row['actual_committed_writes']-row['acknowledged_writes']==row['uncertain_committed_writes']>=0
        assert row['fault_at']<row['promoted_at']<=row['all_workers_recovered_at']
        assert row['all_workers_recovery_seconds']==row['all_workers_recovered_at']-row['fault_at']
        path=root/f'c{count}-r{row["repetition"]}-{row["target"]}'
        operations=read(path/'operations.json')
        assert len(operations)==row['operations']
        assert len({(event['worker'],event['sequence']) for event in operations})==len(operations)
        prefault=[event for event in operations if event['finished']<row['fault_at']]
        assert {event['worker'] for event in prefault if event['write'] and event['outcome']=='ok'}==set(range(count))
        stable=[event for event in operations if event['started']>=row['all_workers_recovered_at']]
        assert all(event['outcome']=='ok' for event in stable)
        assert {event['worker'] for event in stable if event['write']}==set(range(count))
        assert read(path/'observations.json'),'missing fault resource observations'
    return {'arms':24,'unexpected_errors':0,'application_replays':0,'runs':rows,
            'interpretation':'Expected connection/query failures during the injected outage stay explicit. Every worker acknowledges new writes after promotion. Ambiguous committed writes are reconciled, never silently retried.'}


def memory(root):
    rows=read(root/'results.json')
    exact(rows,itertools.product((10,100),MEMORY_TARGETS),lambda row:(row['clients'],row['target']))
    assert read(root/'environment.json')['suppressions'] is False
    for row in rows:
        assert row['queries']==row['clients']*10 and row['errors']==0
        assert row['audit']['definite_indirect_possible_or_access_errors']==0
        assert row['audit']['private_response_findings']==0
    return {'arms':8,'not_normal_timing':True,'runs':rows}


def native(log):
    text=log.read_text()
    assert re.search(r'test result: ok\. 66 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out;',text)
    assert re.search(r'^Ran 73 tests in ',text,re.M) and re.search(r'^OK$',text,re.M)
    complexity=re.search(r'Complexity: .*maximum (\d+).*limit (\d+)\.',text)
    assert complexity and int(complexity[1])<=int(complexity[2])==30
    assert not re.search(r'^FAILED|^ERROR:|^FAIL:|^OK \(',text,re.M)
    return {'rust':66,'offline_python':73,'skips':0,'failures':0,'complexity_maximum':int(complexity[1]),'complexity_limit':30}


def kind(root):
    data=read(root/'results.json')
    assert data['accepted'] is True
    assert data['summary']=={'passed':6,'skipped':0,'failed':0,'error':0,'expected_failure':0,'unexpected_success':0}
    assert all(value==0 for value in data['diagnostic_counts'].values())
    assert len(data['results'])==6 and all(row['status']=='passed' for row in data['results'])
    return {'cases':6,'summary':data['summary'],'contract':data['contract']}


def slow_receivers(root,pool_before):
    directories={'before':pool_before.parent/'protocol-ownership-accepted-resources',
                 'after':root/'heads-resources'}
    result=[]
    for phase,directory in directories.items():
        for count in (10,100):
            path=directory/f'test_{count}_clients_mixed_sizes_saturation_and_small_query_progress'/'memory-cycles.json'
            cycles=read(path)
            assert [row['cycle'] for row in cycles]==[0,1,2]
            for row in cycles:
                assert row['clients']==count
                assert len(row['small_query_latencies_ms'])==count//2*5
                assert 0<row['charged_bytes']<=row['highwater_bytes']<=33554432
                assert row['allocated_bytes']<=33554432
                assert row['after_disconnect_allocated_bytes']==33554432
            result.append({'candidate':phase,'clients':count,'cycles':cycles})
    return {'experiments':4,'cycles_per_experiment':3,'runs':result,
        'interpretation':'Functional barrier experiments on the unshared resource path: database execution completed before held frontend delivery, every small result was checked, and cancellation/deadline/disconnect refunds passed. Large work may be explicitly rejected at the byte limit. These are not matched-core shared-pool timing or a scheduling speedup estimate.'}


def profile_inventory(root):
    original=read(root/'normalized-after/profiles.json')
    exact(original,itertools.product(('cpu','offcpu','heap','syscalls'),(10,100),
              ('rustybouncer-session','rustybouncer-routed')),
          lambda row:(row['mode'],row['clients'],row['target']))
    assert all(row['errors']==0 and row['queries']>0 for row in original)
    expected=list(itertools.product(s.PLACEMENTS,('cpu','offcpu','syscalls'),(100,1000),
              ('rustybouncer-pool-session','rustybouncer-pool-transaction')))
    for phase in ('before','after'):
        pooled=read(root/f'profiles-{phase}'/'profiles.json')
        exact(pooled,expected,lambda row:(row['placement'],row['mode'],row['clients'],row['target']))
        assert all(row['errors']==0 and row['transactions']>=row['clients'] for row in pooled)
    summaries={phase:read(root/f'profiles-{phase}-summary.json') for phase in ('before','after')}
    assert all(value['count']==24 and len(value['profiles'])==24 for value in summaries.values())
    original_summary=read(root/'final-profile-summary.json')
    assert len(original_summary['profiles'])==16
    return {'original_arms':16,'pool_before_arms':24,'pool_after_arms':24,'not_normal_timing':True,
            'pool_summaries':summaries,'original_summary':original_summary}


def provenance(root,pool_before):
    before=read(root/'original-before/environment.json')['binary_sha256']
    after=read(root/'heads-frozen/provenance.json')['production']
    assert read(pool_before/'environment.json')['binary_sha256']==before
    for directory in ('pools-after','original-after','workloads-after','normalized-after','failover-load'):
        assert read(root/directory/'environment.json')['binary_sha256']==after,directory
    assert read(root/'pool-mixed/environment.json')['binary_sha256']=={'before':before,'after':after}
    profile=read(root/'profile-after-frozen/provenance.json')
    assert read(root/'normalized-after/environment.json')['profile_sha256']==profile['profile_sha256']
    assert read(root/'memory-after/environment.json')['binary_sha256']==profile['profile_sha256']
    assert read(root/'profiles-after/environment.json')['production_sha256']==after
    assert read(root/'profiles-after/environment.json')['binary_sha256']==profile['profile_sha256']
    assert read(root/'profiles-before/environment.json')['production_sha256']==before
    return {'production_before_sha256':before,'production_after_sha256':after,
            'profile_after_sha256':profile['profile_sha256'],'profile_runtime_source_commit':'14f077ea6afea7169d48616c9adba97e798e5ed4',
            'harness_checkpoint':'e4e99687579f4cc254711815d36c4bb05f5fafb9',
            'report_source_checkout':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
            'report_generator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def assemble(root,pool_before):
    before=read(pool_before/'results.json')
    after=read(root/'pools-after/results.json')
    live=read(root/'heads-live-summary.json')
    assert live['live_cases']==144
    assert all(live[key]==0 for key in ('failures','errors','skips','expected_failures','unexpected_successes'))
    return {'provenance':provenance(root,pool_before),'unrestricted_lookup_before':matrix(read(root/'original-before/results.json')),
        'unrestricted_lookup_after':matrix(read(root/'original-after/results.json')),
        'normalized_lookup_after':matrix(read(root/'normalized-after/normalized.json')),
        'pools_before':s.report(before,'pools'),'pools_after':s.report(after,'pools'),
        'pool_after_over_before':delta(before,after,'pools'),
        'additional_workloads':s.report(read(root/'workloads-after/results.json'),'workloads'),
        'shared_pool_mixed':mixed(read(root/'pool-mixed/results.json')),'fault_load':faults(root/'failover-load'),
        'memory':memory(root/'memory-after'),'profiles':profile_inventory(root),
        'native':native(root/'final-native.log'),'live':live,'kind':kind(root/'kind-after'),
        'slow_receivers':slow_receivers(root,pool_before),
        'uncertainty':'Every repetition retained. Three shared-host runs give descriptive ranges, CV and paired repetition ratios; no outlier removal, causal isolation, statistical significance or production SLO is established. Before/after batches are separated in time. Instrumented observations never stand in for normal timing. Broader canonical release criteria remain open.'}


def lookup_markdown(groups,title):
    lines=['## '+title,'',
        '| Clients | Target | Median Q/s | Q/s min–max | CV % | p50 ms | p95 ms | p99 ms | Peak backends |',
        '| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for group in groups:
        tps=group['tps']
        lines.append(f'| {group["clients"]} | {group["target"]} | {tps["median"]:,.0f} | '
            f'{tps["min"]:,.0f}–{tps["max"]:,.0f} | {tps["sample_cv"]*100:.2f} | '
            f'{group["p50_ms"]["median"]:.3f} | {group["p95_ms"]["median"]:.3f} | '
            f'{group["p99_ms"]["median"]:.3f} | {group["backend_connections_peak"]["max"]:g} |')
    return '\n'.join(lines)+'\n'


def markdown(report):
    sections=['# Final performance comparison','',report['uncertainty'],'',
              'Runtime source: `14f077e`; complete harness checkpoint: `e4e9968`.','',
              'Original `rustybouncer-session` means passthrough, and `rustybouncer-routed` means unshared routing. Only `rustybouncer-pool-*` enables shared pooling.','',
              'All 30 before +30 final original lookup arms, 30 equal-core arms, 144 before +144 final pool arms, '
              '180 additional workload arms and 96 shared-pool mix arms are retained. '
              'All normal measured transactions report zero query errors. '
              'The separate 24 fault experiments retain expected outage errors and require zero unexpected errors.',
              '',lookup_markdown(report['unrestricted_lookup_before'],'Original lookup before scheduling'),
              '',lookup_markdown(report['unrestricted_lookup_after'],'Original lookup after scheduling'),
              '',lookup_markdown(report['normalized_lookup_after'],'Original lookup with one proxy CPU'),
              '',s.markdown(report['pools_after']),'',s.markdown(report['additional_workloads'])]
    for phase in ('before','after'):
        summary={'kind':'workloads','groups':report['shared_pool_mixed'][phase],
                 'interpretation':f'Shared-pool short/long mix, {phase} candidate. Full logging and per-client completion; all repetitions retained.'}
        sections+=['',s.markdown(summary).replace('# Additional workloads',f'## Shared-pool duration mix ({phase})',1)]
    return '\n'.join(sections)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root',type=Path,required=True)
    parser.add_argument('--pools-before',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=assemble(args.run_root.resolve(),args.pools_before.resolve())
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    (args.output/'summary.md').write_text(markdown(report))
    template=Path(__file__).with_name('comparison.template.html').read_text()
    chart={'before':report['pools_before']['groups'],'after':report['pools_after']['groups']}
    (args.output/'comparison.html').write_text(template.replace('__DATA__',json.dumps(chart).replace('</','<\\/')))
    (args.output/'generator.sha256').write_text(hashlib.sha256(Path(__file__).read_bytes()).hexdigest()+'\n')


if __name__=='__main__':
    main()
