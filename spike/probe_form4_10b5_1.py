"""Phase 0.5 — probe Form 4 for 10b5-1 indicator access.

The 10b5-1 checkbox is the cornerstone of the correlator's plan-vs-discretionary
filter (CLAUDE.md §4.4, ARCHITECTURE.md §5). It only exists on Form 4s filed on
or after 2023-04-01. The Karp 2024 sales (post-April-2023) should have it set
since they were widely reported as plan-driven.

This script checks: does edgartools surface the checkbox, or do we have to
parse raw XML / free-text remarks ourselves?
"""
from __future__ import annotations
import sys
from edgar import find as find_filing, set_identity

set_identity("Redline hatcher.ry@northeastern.edu")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def head(s): print(f"\n{'=' * 72}\n{s}\n{'=' * 72}")
def trunc(s, n=600):
    if s is None: return "(None)"
    s = str(s)
    return s[:n] + (" [...]" if len(s) > n else "")


# One of Karp's Nov 2024 sales (large, recent — should be plan-driven and post-2023-04-01)
f = find_filing("0001321655-24-000220")  # Karp 2024-11-15
print(f"Filing date: {f.filing_date}")
form4 = f.obj()

# 1. Direct attrs
head("1. Form4 attrs that might indicate 10b5-1")
candidate_attrs = [a for a in dir(form4) if "10" in a.lower() or "rule" in a.lower() or "plan" in a.lower() or "trading" in a.lower()]
print(f"  attrs containing 10/rule/plan/trading: {candidate_attrs}")

# 2. Footnotes
head("2. Footnotes / remarks")
print(f"  footnotes type: {type(form4.footnotes).__name__}")
print(f"  footnotes value: {trunc(form4.footnotes, 800)}")
print(f"  remarks type: {type(form4.remarks).__name__}")
print(f"  remarks value: {trunc(form4.remarks, 800)}")

# 3. Raw XML access
head("3. Raw XML — look for rule10b5_1Flag or similar")
try:
    # f.xml might give us the raw filing XML
    xml = f.xml() if callable(getattr(f, "xml", None)) else getattr(f, "xml", None)
    if xml:
        print(f"  xml type: {type(xml).__name__}  len={len(str(xml))}")
        # Search for known XBRL-style tags
        xml_str = str(xml)
        for needle in ["10b5-1", "10b5_1", "rule10b5", "tradingPlan", "writtenPlan", "rule10b5_1Flag", "noTradingPlanFlag"]:
            if needle.lower() in xml_str.lower():
                idx = xml_str.lower().find(needle.lower())
                print(f"  HIT '{needle}' at idx {idx}:")
                print(f"    context: {trunc(xml_str[max(0, idx-100):idx+200], 400)}")
            else:
                print(f"  miss '{needle}'")
    else:
        print("  no xml attribute / call returned None")
except Exception as e:
    print(f"  xml access err: {type(e).__name__}: {e}")

# 4. attachments / primary_document
head("4. attachments — XML primary doc?")
try:
    atts = f.attachments
    print(f"  type: {type(atts).__name__}")
    for a in list(atts)[:10]:
        print(f"    {a}")
except Exception as e:
    print(f"  err: {e}")

# 5. to_dataframe column inspection — maybe there's a hidden col we ignored
head("5. to_dataframe — full column listing")
df = form4.to_dataframe()
print(f"  shape: {df.shape}")
print(f"  columns: {list(df.columns)}")
print(f"  dtypes:")
for col, dt in df.dtypes.items():
    print(f"    {col}: {dt}")

# 6. derivative_table / non_derivative_table — different shape?
head("6. derivative_table / non_derivative_table")
for attr in ("derivative_table", "non_derivative_table"):
    t = getattr(form4, attr, None)
    print(f"  {attr}: type={type(t).__name__}")
    if t is not None:
        print(f"    attrs: {[a for a in dir(t) if not a.startswith('_')][:25]}")
        if hasattr(t, "to_dataframe"):
            try:
                tdf = t.to_dataframe()
                print(f"    df columns: {list(tdf.columns)}")
                print(f"    df shape: {tdf.shape}")
            except Exception as e:
                print(f"    df err: {e}")

# 7. parse_xml / from_xml — maybe raw access
head("7. parse_xml / from_xml — class methods?")
print(f"  parse_xml callable: {callable(getattr(form4, 'parse_xml', None))}")
print(f"  from_xml callable: {callable(getattr(form4, 'from_xml', None))}")

# 8. SGML / homepage / index — find the actual .xml URL
head("8. EntityFiling SGML / homepage — locate raw XML file")
print(f"  homepage_url: {f.homepage_url}")
print(f"  primary_documents (sample): {list(f.primary_documents)[:5] if hasattr(f.primary_documents, '__iter__') else f.primary_documents}")
