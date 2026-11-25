import pyflp, json, sys

path = sys.argv[1]
proj = pyflp.parse(path)
print("type:", type(proj).__name__)
print("attrs:", [a for a in dir(proj) if not a.startswith("_")][:30])

pats = getattr(proj, "patterns", None)
if pats is None:
    raise SystemExit("no patterns attr")
print("patterns:", len(pats))
for p in pats:
    name = getattr(p, "name", "?")
    print("--- pattern:", repr(name), "| attrs:", [a for a in dir(p) if not a.startswith("_")][:25])
