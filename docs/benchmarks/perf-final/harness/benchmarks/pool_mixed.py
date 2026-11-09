#!/usr/bin/env python3
"""PERF-005: 96 before/after small-pool short/long fairness observations.

Two frozen candidates x four poolers x 10/100 clients x two CPU placements x
three repetitions. All arms, all clients and complete logs are mandatory.
"""
import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from benchmarks.performance_profile import dump  # noqa: E402
from benchmarks.pool_compare import CLIENT_CAPACITY, POOL_SIZE, TARGETS, PoolFixture  # noqa: E402
from benchmarks.workload_compare import SCRIPTS, arm  # noqa: E402


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--before-binary',type=Path,required=True)
    parser.add_argument('--after-binary',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    pg=shutil.which('pgbouncer')
    assert pg
    os.environ['PERF_PGBOUNCER_BIN']=pg
    binaries={'before':args.before_binary.resolve(),'after':args.after_binary.resolve()}
    shutil.copyfile(__file__,output/'harness.py')
    dump(output/'environment.json',{'command':sys.argv,'arms':96,'pool_size':POOL_SIZE,
        'client_capacity':CLIENT_CAPACITY,'clients':[10,100],'repetitions':3,
        'binary_sha256':{name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in binaries.items()},
        'placements':['unrestricted','normalized'],'warmup_seconds':5,'measurement_seconds':10,
        'mixed_weights':{'indexed_lookup':9,'pg_sleep_20ms':1},
        'logging':'100%; all clients must complete; short/long distributions and maximum latency separate',
        'safety':'no SQL retry, every frontend independently verifies full bytes and identity before timing',
        'limitation':'shared host, three repetitions; before/after is not interleaved in time or causal significance'})
    results=[]
    for version,binary in binaries.items():
        for placement in ('unrestricted','normalized'):
            directory=output/version/placement
            directory.mkdir(parents=True)
            for name,sql in SCRIPTS.items():
                (directory/f'{name}.sql').write_text(sql)
            fixture=PoolFixture(directory,binary,mode=placement,cpu=min(os.sched_getaffinity(0)))
            try:
                fixture.start()
                for clients in (10,100):
                    for repetition in (1,2,3):
                        offset=(repetition-1+int(clients==100))%len(TARGETS)
                        targets=TARGETS[offset:]+TARGETS[:offset]
                        if repetition%2==0:
                            targets=tuple(reversed(targets))
                        for target in targets:
                            result=arm(fixture,directory,clients,repetition,target,'mixed')
                            assert result['backend_connections_peak']<=POOL_SIZE,result
                            result.update({'candidate':version,'pool_size':POOL_SIZE})
                            dump(directory/f'c{clients}-r{repetition}-{target}'/'result.json',result)
                            results.append(result)
                            dump(output/'results.json',results)
                            print(f'{len(results)}/96 {version}/{placement} {target} c={clients}',flush=True)
            finally:
                fixture.close()
    assert len(results)==96


if __name__=='__main__':
    main()
