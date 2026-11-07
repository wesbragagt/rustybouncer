"""Endpoint-only runner. Exit nonzero for failures, skips, expected failures or invalid configuration."""
import argparse
import dataclasses
import sys
import unittest
from pathlib import Path

from .base import Case
from .concurrency import Concurrency
from .config import Contract, endpoints
from .lifecycle import Lifecycle
from .limits import Limits
from .pooling import Pooling
from .protocol import Protocol
from .recovery import Recovery
from .reporting import Result
from .security import Security
from .transactions import Transactions

CASES = (Pooling, Transactions, Protocol, Limits, Security, Lifecycle, Recovery, Concurrency)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json", type=Path, default=Path("conformance-results.json"))
    parser.add_argument("--junit", type=Path, default=Path("conformance-results.xml"))
    parser.add_argument("--match", default="")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat must be positive")
    try:
        Case.contract = Contract.load(args.manifest)
        if not args.list:
            Case.pooler_dsn, Case.postgres_dsn = endpoints()
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for _ in range(args.repeat):
        for cls in CASES:
            suite.addTests(cls(name) for name in loader.getTestCaseNames(cls) if args.match in cls(name).id())
    if not suite.countTestCases():
        parser.error("test filter matched no cases")
    if args.list:
        for test in suite:
            assert isinstance(test, unittest.TestCase)
            print(test.id())
        return 0
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    assert isinstance(result, Result)
    result.write(args.json, args.junit, dataclasses.asdict(Case.contract),
                 full_inventory=not bool(args.match))
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    sys.exit(main())
