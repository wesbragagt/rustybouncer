#!/usr/bin/env python3
"""Summarize all 24 pool profiles, with explicit measured-window CPU attribution."""
import argparse
import itertools
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from benchmarks.profile_report import switches  # noqa: E402


def category(stack):
    rules=(
        ('pool_admission_handoff',('router::pool::Manager','router::pool::Ticket','router::pool::Slot','router::pool::State')),
        ('session_affinity_analysis',('router::affinity::',)),
        ('query_classification',('router::classify','router::classification::','router::cte::')),
        ('cancellation_control',('rustybouncer::cancel::',)),
        ('bulk_buffer_accounting',('rustybouncer::buffers::',)),
        ('frame_write',('router::wire::send',)),
        ('response_delivery',('router::wire::Response>::write_to',)),
        ('frame_read_assembly',('router::wire::receive','router::wire::response','router::message','router::result')),
        ('metrics',('rustybouncer::metrics::',)),
        ('topology_discovery',('rustybouncer::discover','rustybouncer::probe')),
        ('runtime_lifecycle_or_other',()),
    )
    for name,needles in rules:
        if not needles or any(needle in stack for needle in needles):
            return name
    raise AssertionError('unreachable')


def cpu(path,started,ended):
    counts,weights,self_weights=Counter(),Counter(),Counter()
    frames=[]
    period=0
    def finish():
        if not period:
            return
        name=category('\n'.join(frames))
        counts[name]+=1
        weights[name]+=period
        self_weights[frames[0] if frames else 'unresolved']+=period
    with path.open() as source:
        for line in source:
            if 'cpu-clock:u:' in line or 'PERF_RECORD_SWITCH ' in line:
                finish()
                frames,period=[],0
                if 'cpu-clock:u:' in line:
                    match=re.search(r'\s([\d.]+):\s+(\d+)\s+cpu-clock:u:',line)
                    assert match,'missing monotonic timestamp or sampling period'
                    if started<=float(match[1])<=ended:
                        period=int(match[2])
            elif period and line.strip():
                frames.append(re.sub(r'^[0-9a-f]+\s+','',line.strip()))
    finish()
    total=sum(weights.values())
    assert total>0 and sum(counts.values())>0,'no in-window samples'
    return {'samples':sum(counts.values()),'period_weight':total,
        'categories':[{'name':name,'samples':counts[name],'period_share':value/total} for name,value in weights.most_common()],
        'self_symbols':[{'symbol':name,'period_share':value/total} for name,value in self_weights.most_common(40)],
        'interpretation':'Exclusive user callchain buckets in listed precedence, clipped to the controlled load window. Pool includes admission/handoff bookkeeping; cancellation includes repeated control polling. Kernel execution and unresolved/inlined ancestors limit attribution. Shares are not isolated marginal elapsed costs.'}


def analyze(root):
    rows=json.loads((root/'profiles.json').read_text())
    expected=set(itertools.product(('unrestricted','normalized'),('cpu','offcpu','syscalls'),(100,1000),
        ('rustybouncer-pool-session','rustybouncer-pool-transaction')))
    actual=[(row['placement'],row['mode'],row['clients'],row['target']) for row in rows]
    assert len(actual)==len(expected) and set(actual)==expected,'incomplete/duplicate profile matrix'
    result=[]
    for row in rows:
        assert row['errors']==0 and row['transactions']>=row['clients'],row
        path=root/row['placement']/row['mode']/f'c{row["clients"]}-{row["target"]}'
        item=dict(row)
        if row['mode']=='cpu':
            item['cpu']=cpu(path/'stacks.txt',row['measurement_started_monotonic'],row['measurement_finished_monotonic'])
        elif row['mode']=='offcpu':
            item['switches']=switches(path/'stacks.txt',row['measurement_started_monotonic'],row['measurement_finished_monotonic'])
        else:
            assert row['transactions']==row['clients']*2
            counts=json.loads((path/'syscalls.json').read_text())
            item['lifetime_syscalls_per_controlled_transaction']={name:{**values,'calls_per_transaction':values['calls']/row['transactions']}
                for name,values in counts.items()}
        result.append(item)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=analyze(args.profiles)
    args.output.write_text(json.dumps({'profiles':result,'count':len(result),'not_normal_timing':True},indent=2,allow_nan=False)+'\n')


if __name__=='__main__':
    main()
