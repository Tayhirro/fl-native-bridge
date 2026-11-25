"""Verify an isolated native build without FL Studio.

Checks the three things CI must prove for a compiled .pyd:
  1. CPython 3.12 can import it.
  2. The operation table matches the expected count and includes the gated
     write operations.
  3. With no FL engine loaded, initialize() and execute() both refuse.

Usage: python scripts/verify-native-build.py <path-to-.pyd>
"""

import sys
from pathlib import Path

EXPECTED_OPERATIONS = 35
GATED_WRITES = ("mixer.insert_empty_slot", "mixer.remove_effect", "project.save_copy")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1]).resolve(strict=True)
    sys.path.insert(0, str(path.parent))
    import importlib

    module = importlib.import_module(path.stem)
    print(f"PASS imported {path.name}")

    described = module.describe()
    operations = described.get("operations") or []
    if len(operations) != EXPECTED_OPERATIONS:
        print(f"FAIL operation count: {len(operations)} != {EXPECTED_OPERATIONS}")
        return 1
    missing = [op for op in GATED_WRITES if op not in operations]
    if missing:
        print(f"FAIL gated writes absent from table: {missing}")
        return 1
    dependencies = described.get("dependencies") or {}
    missing_deps = [op for op in GATED_WRITES if op not in dependencies]
    if missing_deps:
        print(f"FAIL gated writes absent from dependency map: {missing_deps}")
        return 1
    print(f"PASS operation table: {len(operations)} operations, {len(dependencies)} gated")

    try:
        module.initialize()
    except RuntimeError as exc:
        print(f"PASS initialize refused: {exc}")
    else:
        print("FAIL initialize succeeded without an engine")
        return 1

    for operation in GATED_WRITES:
        args = (str(path),) if operation == "project.save_copy" else (str(path), 0, 0)
        try:
            module.execute(operation, args)
        except RuntimeError as exc:
            print(f"PASS {operation} refused: {exc}")
        else:
            print(f"FAIL {operation} executed without an engine")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
