#!/usr/bin/env python3
"""PERF-005 additional attribution: 24 shared-pool profiles, never normal timing.

Two Rust pool modes x 100/1,000 actual clients x CPU/off-CPU/syscalls
x unrestricted/one-proxy-CPU placements.
The complete original 16-profile matrix remains a separate required scope.
"""
import argparse
import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.performance_profile import dump, process_info, report_profile  # noqa: E402
from benchmarks.pool_compare import (  # noqa: E402
    CLIENT_CAPACITY,
    POOL_SIZE,
    TRANSACTION_SCRIPT,
    PoolFixture,
    preflight,
)

TARGETS = ('rustybouncer-pool-session', 'rustybouncer-pool-transaction')


class ProfileFixture(PoolFixture):
    def __init__(self,*args,placement,**kwargs):
        super().__init__(*args,**kwargs)
        self.placement=placement

    def command(self,command,directory):
        if self.placement=='normalized':
            command=['taskset','-c',str(self.cpu)]+command
        return super().command(command,directory)

    def launch_target(self,target,directory):
        if self.placement=='normalized':
            with patch.dict(os.environ,{'TOKIO_WORKER_THREADS':'1'}):
                super().launch_target(target,directory)
        else:
            super().launch_target(target,directory)

    def launch(self,target,directory):
        super().launch(target,directory)
        assert self.target_pid is not None
        snapshot=process_info(self.target_pid)
        if self.placement=='normalized':
            assert snapshot['threads'] and all(thread['affinity']==[self.cpu] for thread in snapshot['threads'])
            assert sum(thread['name'].startswith(('tokio-rt-worker','tokio-runtime')) for thread in snapshot['threads'])==1
        dump(directory/'profile-placement.json',{'placement':self.placement,'snapshot':snapshot})


def arm(fixture, clients, target, directory):
    directory.mkdir()
    fixture.target_pid=None
    fixture.launch(target,directory)
    asyncio.run(preflight(fixture,clients,directory))
    command=['pgbench','-h','127.0.0.1','-p',str(fixture.proxy_port),'-U','postgres','-d','postgres','-n',
             '--max-tries=1','--exit-on-abort','-M','simple','-c',str(clients),'-j',str(min(16,clients)),
             '-C','-f',str(directory.parent/'transaction.sql')]
    command += ['-t','2'] if fixture.mode=='syscalls' else ['-T','10']
    dump(directory/'load-command.json',command)
    started,epoch_started=time.monotonic(),time.time()
    completed=subprocess.run(command,text=True,capture_output=True,timeout=180,check=False)
    ended,epoch_ended=time.monotonic(),time.time()
    (directory/'pgbench.txt').write_text(completed.stdout+completed.stderr)
    assert completed.returncode==0,completed.stdout+completed.stderr
    transactions=re.search(r'number of transactions actually processed:\s*(\d+)',completed.stdout)
    errors=re.search(r'number of failed transactions:\s*(\d+)',completed.stdout)
    assert transactions and errors and int(errors[1])==0
    count=int(transactions[1])
    assert count>=clients
    if fixture.mode=='syscalls':
        assert count==clients*2
    result={'placement':fixture.placement,'mode':fixture.mode,'clients':clients,'target':target,'transactions':count,'errors':0,
            'measurement_started_monotonic':started,'measurement_finished_monotonic':ended,
            'measurement_started_epoch':epoch_started,'measurement_finished_epoch':epoch_ended,
            'profile_seconds':ended-started,'not_timing_comparable':True,
            'pool_size':POOL_SIZE,'client_capacity':CLIENT_CAPACITY,'workload':'churn, BEGIN/guarded lookup/COMMIT',
            'amortization_scope':'profiler lifetime includes real prepared preflight, setup/probes and controlled load; raw time windows retained'}
    fixture.finish(directory,result)
    report_profile(fixture,directory)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--profile-binary',type=Path,required=True)
    parser.add_argument('--production-binary',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    binary=args.profile_binary.resolve()
    sections=subprocess.check_output(['readelf','--sections',str(binary)],text=True)
    assert '.debug_info' in sections,'profile binary must retain debug information'
    shutil.copyfile(__file__,output/'harness.py')
    dump(output/'environment.json',{'command':sys.argv,'binary':str(binary),
         'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
         'production_sha256':hashlib.sha256(args.production_binary.read_bytes()).hexdigest(),
         'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
         'source_status':subprocess.check_output(['git','status','--short'],text=True),
         'harness_affinity':sorted(os.sched_getaffinity(0)),'profile_arms':24,
         'placements':['unrestricted','normalized'],'clients':[100,1000],'targets':TARGETS,'modes':['cpu','offcpu','syscalls'],
         'limitation':'instrumented attribution only; not throughput evidence; user-mode samples/switch records are not complete kernel scheduling traces'})
    results=[]
    for placement in ('unrestricted','normalized'):
        for mode in ('cpu','offcpu','syscalls'):
            directory=output/placement/mode
            directory.mkdir(parents=True)
            (directory/'transaction.sql').write_text(TRANSACTION_SCRIPT)
            fixture=ProfileFixture(directory,binary,mode=mode,cpu=min(os.sched_getaffinity(0)),placement=placement)
            try:
                fixture.start()
                for clients in (100,1000):
                    for target in TARGETS:
                        result=arm(fixture,clients,target,directory/f'c{clients}-{target}')
                        results.append(result)
                        dump(output/'profiles.json',results)
                        print(f'{len(results)}/24 {placement}/{mode} {target} c={clients}',flush=True)
            finally:
                fixture.close()
    assert len(results)==24


if __name__=='__main__':
    main()
