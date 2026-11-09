#!/usr/bin/env python3
"""PERF-001 complete 16-arm attribution matrix, separate from normal timing."""
import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.performance_profile import Instrumented, dump, profile_arm  # noqa: E402


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--binary',type=Path,required=True)
    args=parser.parse_args()
    root=args.output.resolve()
    root.mkdir(parents=True,exist_ok=False)
    binary=args.binary.resolve()
    reference=shutil.which('pgbouncer')
    assert reference
    os.environ['PERF_PGBOUNCER_BIN']=reference
    shutil.copyfile(__file__,root/'harness.py')
    shutil.copyfile(Path(__file__).with_name('performance_profile.py'),root/'performance_profile.py')
    dump(root/'environment.json',{'binary':str(binary),'sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
         'command':sys.argv,'clock':'perf --clockid mono = Python CLOCK_MONOTONIC',
         'attribution_arms':16,'not_timing_comparable':True})
    (root/'select.sql').write_text('\\set id random(1, 10000)\nSELECT payload FROM comparison WHERE id = :id;\n')
    rows=[]
    for mode in ('cpu','offcpu','heap','syscalls'):
        directory=root/mode
        directory.mkdir()
        fixture=Instrumented(directory,binary,mode=mode,cpu=min(os.sched_getaffinity(0)))
        try:
            fixture.start()
            for clients in (10,100):
                for target in ('rustybouncer-session','rustybouncer-routed'):
                    rows.append(profile_arm(fixture,clients,target,directory/f'c{clients}-{target}'))
                    dump(root/'profiles.json',rows)
                    print(f'{len(rows)}/16 {mode} {clients} {target}',flush=True)
        finally:
            fixture.close()
    assert len(rows)==16


if __name__=='__main__':
    main()
