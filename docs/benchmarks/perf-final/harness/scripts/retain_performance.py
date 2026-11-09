#!/usr/bin/env python3
"""Retain the complete accepted PERF-005/009 evidence; never archive active runs."""
import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REQUIRED=('pools-after','original-after','workloads-after','pool-mixed','normalized-after',
          'profiles-after','memory-after','failover-load','kind-after')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def archive(source,output,name):
    manifest={}
    for path in sorted(source.rglob('*')):
        relative=str(path.relative_to(source))
        if path.is_symlink():
            manifest[relative]={'symlink':os.readlink(path)}
        elif path.is_file():
            manifest[relative]={'bytes':path.stat().st_size,'sha256':digest(path)}
        else:
            assert path.is_dir(),f'unexpected special evidence file: {path}'
    assert manifest,f'empty evidence scope: {source}'
    with gzip.open(output/f'{name}-manifest.json.gz','wt') as stream:
        json.dump(manifest,stream,indent=2)
    target=output/f'{name}.tar.zst'
    subprocess.run(['tar','--zstd','-cf',str(target),'-C',str(source),'.'],check=True)
    return {'path':target.name,'files':len(manifest),'bytes':target.stat().st_size,'sha256':digest(target)}


def freeze_binaries(args,root,output):
    binaries={'before-production':args.before_production,'before-profile':args.before_profile,
              'after-production':root/'heads-frozen/production',
              'after-profile':root/'profile-after-frozen/rustybouncer'}
    with tempfile.TemporaryDirectory(prefix='rustybouncer-frozen-elf-') as temporary:
        directory=Path(temporary)
        hashes={}
        for name,path in binaries.items():
            shutil.copy2(path,directory/name)
            hashes[name]=digest(directory/name)
        provenance=json.loads((root/'final-report/report.json').read_text())['provenance']
        assert hashes['before-production']==provenance['production_before_sha256']
        assert hashes['after-production']==provenance['production_after_sha256']
        assert hashes['after-profile']==provenance['profile_after_sha256']
        assert hashes['before-profile']==json.loads((root/'profiles-before/environment.json').read_text())['binary_sha256']
        (directory/'sha256.json').write_text(json.dumps(hashes,indent=2)+'\n')
        return archive(directory,output,'frozen-binaries')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root',type=Path,required=True)
    parser.add_argument('--before-production',type=Path,required=True)
    parser.add_argument('--before-profile',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root,output=args.run_root.resolve(),args.output.resolve()
    # The report generator checks every matrix, source binding and zero-skip
    # functional scope before producing this directory. No partial report exists.
    report=root/'final-report'
    assert (report/'report.json').is_file(),'complete validated final report required'
    for name in REQUIRED:
        assert (root/name).is_dir(),f'missing required evidence scope: {name}'
    shutil.copytree(report,output)
    archives=[archive(root/name,output,name) for name in REQUIRED]
    archives.append(freeze_binaries(args,root,output))
    for directory in sorted(root.glob('diagnostic-*')):
        if directory.is_dir():
            item=archive(directory,output,directory.name)
            item['diagnostic_only']=True
            archives.append(item)
    metadata=output/'run-metadata'
    metadata.mkdir()
    for pattern in ('*.log','*.json','*.jsonl','*.sh'):
        for path in sorted(root.glob(pattern)):
            shutil.copy2(path,metadata/path.name)
    harness=output/'harness'
    harness.mkdir()
    for directory in ('benchmarks','tests','conformance','scripts'):
        destination=harness/directory
        destination.mkdir()
        for path in sorted(Path(directory).glob('*.py')):
            shutil.copy2(path,destination/path.name)
    for name in ('comparison.template.html','README.md'):
        shutil.copy2(Path('benchmarks')/name,harness/'benchmarks'/name)
    for name in ('flake.nix','flake.lock','pyproject.toml'):
        shutil.copy2(name,harness/name)
    shutil.copy2('scripts/verify',harness/'scripts'/'verify')
    (output/'archives.json').write_text(json.dumps(archives,indent=2)+'\n')
    print(json.dumps({'archives':len(archives),'bytes':sum(row['bytes'] for row in archives),
                      'required_scopes':len(REQUIRED),'output':str(output)}))


if __name__=='__main__':
    main()
