"""Exercise the native dependency gate against an isolated .pyd build.

Build first with scripts/build-native-effect-loader.ps1 into a separate
directory, then run:

  python scripts/verify-native-gate.py <path-to-.pyd>

The test-only hook may only downgrade evidence, so this cannot be used to
weaken a production build. A fake engine pointer lets the gate run without FL;
handlers still refuse execution on the wrong thread, which is how we observe
that the gate itself allowed the call through.
"""

import sys
from pathlib import Path


def load_extension(path: Path):
    # The compiled module exports PyInit__flmcp_native, so it must be imported
    # by its real filename rather than through an arbitrary alias.
    sys.path.insert(0, str(path.parent))
    import importlib

    return importlib.import_module(path.stem)


def refusal(module, operation, args):
    try:
        module.execute(operation, args)
    except RuntimeError as exc:
        text = str(exc)
        return text.split("refused: ", 1)[-1] if "refused: " in text else text
    return "ALLOWED"


def verify(module) -> list[str]:
    checks = []
    save = ("C:/gate-test.flp",)
    mix = ("C:/gate-test.fst", 0, 0)

    def record(label, value, expected):
        ok = expected in value
        checks.append(f"{'PASS' if ok else 'FAIL'} {label}: {value}")
        if not ok:
            raise AssertionError(f"{label}: expected {expected!r}, got {value!r}")

    profile_digest = bytes.fromhex(
        "6d9c5ab09c1d5594012ac2c31cf9ed08e73f42164ed0964cccae10a95d9b8e02")
    checks.append(f"PASS digest gate available: {hasattr(module, 'check_engine_digest')}")
    if not hasattr(module, "check_engine_digest"):
        raise AssertionError("extension has no digest comparison hook")
    if not module.check_engine_digest(profile_digest):
        raise AssertionError("compiled profile digest does not self-match")
    checks.append("PASS digest self-match")
    wrong = bytearray(profile_digest)
    wrong[0] ^= 1
    if module.check_engine_digest(bytes(wrong)) or module.check_engine_digest(profile_digest[:16]):
        raise AssertionError("wrong/short digest was accepted")
    checks.append("PASS wrong and short digests refused")

    record("no engine", refusal(module, "project.save_copy", save),
           "Engine is not initialized")

    module.test_override("project.save_copy", "engine", "fake")
    # The gate passed; the handler's own thread check now refuses.
    record("gate passed to handler", refusal(module, "project.save_copy", save),
           "FL idle thread")

    for field, expected in (
        ("calling_convention", "Calling convention"),
        ("global_addresses", "globals or object layout"),
        ("object_layout", "globals or object layout"),
        ("rva", "RVA is outside"),
    ):
        module.test_override("project.save_copy", field, "unsupported")
        record(f"{field} downgrade", refusal(module, "project.save_copy", save), expected)
        # A single dependency failure must not refuse an unrelated operation.
        record(f"{field} isolation", refusal(module, "mixer.insert_empty_slot", mix),
               "FL idle thread")
        module.test_override("project.save_copy", field, None)

    record("restored", refusal(module, "project.save_copy", save), "FL idle thread")
    module.test_override("project.save_copy", "engine", "unsupported")
    record("engine removed", refusal(module, "project.save_copy", save),
           "Engine is not initialized")
    return checks


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1]).resolve(strict=True)
    module = load_extension(path)
    if not hasattr(module, "test_override"):
        print("FAIL: extension has no test_override hook (stale build?)")
        return 1
    for line in verify(module):
        print(line)
    print(f"Gate verification passed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
