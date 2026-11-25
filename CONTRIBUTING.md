# Contributing

The most useful thing you can send is not a pull request — it's a report that says
which FL build you're on and whether the bridge loaded. The offsets are calibrated to
one version; every other version is a data point I don't have.

## Before anything else: the install report

Open an issue using the **Install report** template. Include:

- FL Studio version (Help → About, the full build string)
- Whether `fl_bridge_status` returns `native: true`
- What failed, verbatim, if it did

That is worth more than a feature request, and it takes two minutes.

## Reporting a bug

Use the **Bug report** template. The three things that make a report actionable:

1. The engine profile string from the tool result (it's in `native.profile`).
2. The exact tool call and its full JSON result, not a paraphrase.
3. Whether the operation is marked `irreversible` — if it is, say whether you had a
   save-copy first.

Do not paste an entire session log. The relevant result object is enough.

## Code contributions

### Python (server, tools, tests)

This is where most contributions are welcome and easiest to review.

```bash
uv sync
uv run pytest -q
uv run ruff check src tests
```

Rules that the existing code follows, and CI enforces:

- A tool that mutates state must read back and report what it observed. Returning
  "ok" without a readback is not accepted.
- Destructive operations return `irreversible: true` when FL has no restorable undo.
  Do not soften that.
- Tests must not depend on a local FL install. Pin `Path.home` and build fixtures in
  `tmp_path`; a test that only passes on the author's machine is a bug.
- No hardcoded absolute paths. Ever.

### Native (`fl_controller/native/*.c`)

Higher bar, because a mistake writes into someone's project.

- Every new operation needs: an argument validation path, an idle-thread guard, a
  readback, and a documented failure mode.
- Version-specific offsets belong in `fl_controller/native/profiles/`, never inline.
- If an operation cannot be undone, say so in the docstring and in the result.
- Build with `scripts/build-native-effect-loader.ps1`. Do not commit dated builds;
  the shipped extension has one fixed name.

If you're adding an engine call, open an issue first with the evidence you have
(what you observed in FL, which build). A patch that works on your machine but
carries no profile data will be asked for the profile data.

### Documentation

Screenshots and corrections to `SEMANTIC_NATIVE.md` / `NATIVE_BRIDGE.md` are welcome.
If a claim in the README is wrong, that's a bug — open an issue.

## What will be declined

- Support for a different FL version without a profile and test evidence.
- Anything that redistributes FL binaries, DLLs, or decompiled listings.
- "Make it work on macOS/Linux" — the extension is Windows x64, and the engine is
  Windows-only.
- Features that bypass Image-Line's licensing or copy protection. This project
  interoperates with a licensed copy; it does not unlock anything.

## Commit messages

Plain and specific. Say what changed and why, in the imperative. If a fix came from a
real failure, name the failure — those messages are the only history this project has.
