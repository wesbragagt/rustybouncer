#!/usr/bin/env python3
"""PERF-003: four separate large-result allocation profiles; no timing claims."""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import compare as c  # noqa: E402
from benchmarks.performance_profile import Instrumented, dump, report_profile  # noqa: E402
from benchmarks.workload_compare import SCRIPTS, oracle  # noqa: E402


def arm(fixture, clients, target, directory):
    directory.mkdir()
    fixture.launch(target,directory)
    c.m.preflight(fixture,clients)
    oracle(fixture,'large')
    command=['pgbench','-h','127.0.0.1','-p',str(fixture.proxy_port),'-U','postgres','-d','postgres',
             '-n','-M','simple','-c',str(clients),'-j',str(clients),'-t','10',
             '-f',str(directory.parent/'large.sql')]
    dump(directory/'command.json',command)
    began=time.monotonic()
    output=subprocess.run(command,text=True,capture_output=True,timeout=120,check=False)
    ended=time.monotonic()
    (directory/'pgbench.txt').write_text(output.stdout+output.stderr)
    count=re.search(r'number of transactions actually processed:\s*(\d+)',output.stdout)
    errors=re.search(r'number of failed transactions:\s*(\d+)',output.stdout)
    assert output.returncode==0 and count and errors, output.stdout+output.stderr
    assert int(count[1])==clients*10 and int(errors[1])==0
    oracle(fixture,'large')
    result={'clients':clients,'target':target,'queries':int(count[1]),'errors':0,'rows_per_q':2048,
            'payload_bytes_per_row':128,'measurement_started_monotonic':began,
            'measurement_finished_monotonic':ended,'not_timing_comparable':True,
            'amortization_scope':'heap lifetime includes startup/preflight and two exact 2048-row oracle queries; controlled pgbench Q count excludes these; report overhead explicitly'}
    fixture.finish(directory,result)
    report_profile(fixture,directory)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--binary',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    reference=shutil.which('pgbouncer')
    assert reference
    os.environ['PERF_PGBOUNCER_BIN']=reference
    binary=args.binary.resolve()
    shutil.copyfile(__file__,output/'harness.py')
    dump(output/'environment.json',{'binary':str(binary),'sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
         'command':sys.argv,'affinity':sorted(os.sched_getaffinity(0)), 'arms':4,
         'workload':'2048 ordered rows with 128-byte payload; 10 transactions per client; instrumented observations only'})
    (output/'large.sql').write_text(SCRIPTS['large'])
    fixture=Instrumented(output,binary,mode='heap',cpu=min(os.sched_getaffinity(0)))
    results=[]
    try:
        fixture.start()
        for clients in (10,100):
            for target in ('rustybouncer-session','rustybouncer-routed'):
                results.append(arm(fixture,clients,target,output/f'c{clients}-{target}'))
                dump(output/'results.json',results)
                print(f'{len(results)}/4 {clients} {target}',flush=True)
    finally:
        fixture.close()
    assert len(results)==4


if __name__=='__main__':
    main()
