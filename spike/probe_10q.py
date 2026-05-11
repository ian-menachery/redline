"""Phase 0.5 — deeper 10-Q section-access probe.

verify.py found that get_item_with_part returns empty for the four sections
we care about, despite the items being listed. This script tries every
plausible access pattern to find the one that actually returns text.
"""
from __future__ import annotations
import sys
from edgar import find as find_filing, set_identity

set_identity("Redline hatcher.ry@northeastern.edu")

# Force UTF-8 stdout so arrows/em-dashes don't crash on Windows cp1252.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def head(s):
    print(f"\n{'=' * 72}\n{s}\n{'=' * 72}")


def trunc(s, n=300):
    if s is None:
        return "(None)"
    s = str(s).strip()
    return s[:n] + (" [...]" if len(s) > n else "")


f = find_filing("0001321655-24-000209")  # PLTR Q3 2024 10-Q
tenq = f.obj()

# ----- 1. .items list -----
head("1. tenq.items list (raw)")
items = tenq.items
print(f"  type: {type(items).__name__}")
print(f"  value: {items}")

# ----- 2. .sections -----
head("2. tenq.sections")
sections = tenq.sections
print(f"  type: {type(sections).__name__}")
print(f"  repr: {trunc(repr(sections), 600)}")
if hasattr(sections, "__iter__"):
    try:
        for i, s in enumerate(list(sections)[:5]):
            print(f"   [{i}] type={type(s).__name__}  repr={trunc(repr(s), 200)}")
    except Exception as e:
        print(f"   iter err: {e}")

# ----- 3. .get_item_with_part with swapped args -----
head("3. get_item_with_part — argument variants")
for args in [
    ("Item 1A", "Part II"),
    ("Part II", "Item 1A"),
    ("Item 2", "Part I"),
    ("Part I", "Item 2"),
    ("1A", "II"),
    ("2", "I"),
]:
    try:
        r = tenq.get_item_with_part(*args)
        print(f"  {args}: type={type(r).__name__}  len={len(str(r)) if r else 0}  preview={trunc(r, 80)}")
    except Exception as e:
        print(f"  {args}: ERROR {type(e).__name__}: {e}")

# ----- 4. .structure -----
head("4. tenq.structure (and get_structure)")
struct = tenq.structure
print(f"  type: {type(struct).__name__}")
print(f"  attrs: {[a for a in dir(struct) if not a.startswith('_')]}")
try:
    gs = tenq.get_structure()
    print(f"  get_structure() -> {type(gs).__name__}")
    print(f"    attrs: {[a for a in dir(gs) if not a.startswith('_')]}")
    if hasattr(gs, "items"):
        print(f"    .items: {gs.items}")
except Exception as e:
    print(f"  get_structure err: {e}")

# ----- 5. .document and .doc -----
head("5. tenq.document / tenq.doc")
for attr in ("document", "doc"):
    d = getattr(tenq, attr, None)
    print(f"  {attr}: type={type(d).__name__}")
    if d is not None:
        print(f"    attrs: {[a for a in dir(d) if not a.startswith('_')][:30]}")
        # Try common accessors
        for m in ("text", "to_text", "get_text", "html", "markdown"):
            v = getattr(d, m, None)
            if callable(v):
                try:
                    out = v()
                    print(f"    {attr}.{m}(): type={type(out).__name__}  len={len(str(out)) if out else 0}  preview={trunc(out, 120)}")
                except Exception as e:
                    print(f"    {attr}.{m}() err: {e}")
            elif v is not None:
                print(f"    {attr}.{m}: type={type(v).__name__}  len={len(str(v)) if v else 0}  preview={trunc(v, 120)}")

# ----- 6. .grep search for known phrase -----
head("6. tenq.grep('Risk Factors') — text search")
try:
    res = tenq.grep("Risk Factors")
    print(f"  type: {type(res).__name__}")
    print(f"  preview: {trunc(res, 600)}")
except Exception as e:
    print(f"  ERROR {type(e).__name__}: {e}")

# ----- 7. .view -----
head("7. tenq.view (callable?)")
v = tenq.view
print(f"  type: {type(v).__name__}  callable={callable(v)}")

# ----- 8. f.markdown / f.text on the EntityFiling itself -----
head("8. EntityFiling-level text / markdown")
try:
    md = f.markdown()
    print(f"  f.markdown(): type={type(md).__name__}  len={len(str(md)) if md else 0}")
    print(f"  preview: {trunc(md, 400)}")
except Exception as e:
    print(f"  f.markdown() err: {e}")
try:
    txt = f.text
    print(f"  f.text: type={type(txt).__name__}  len={len(str(txt)) if txt else 0}")
    print(f"  preview: {trunc(txt, 400)}")
except Exception as e:
    print(f"  f.text err: {e}")

# ----- 9. f.sections (on EntityFiling) -----
head("9. EntityFiling.sections")
try:
    s = f.sections
    print(f"  type: {type(s).__name__}")
    print(f"  preview: {trunc(repr(s), 600)}")
except Exception as e:
    print(f"  err: {e}")

# ----- 10. .chunked_document -----
head("10. tenq.chunked_document")
try:
    cd = tenq.chunked_document
    print(f"  type: {type(cd).__name__}")
    print(f"  attrs: {[a for a in dir(cd) if not a.startswith('_')][:25]}")
    # Try to iterate
    if hasattr(cd, "__iter__"):
        for i, ch in enumerate(list(cd)[:3]):
            print(f"   chunk[{i}] type={type(ch).__name__} preview={trunc(ch, 160)}")
except Exception as e:
    print(f"  err: {e}")
