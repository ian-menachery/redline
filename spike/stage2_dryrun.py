"""Phase 0.5 — Stage 2 dry-run using OpenAI gpt-4o-mini.

Exploratory simulation of the future Anthropic Haiku Stage 2 gate. Reads
the 85 surviving chunks from spike/pltr_10k_riskdiff.md, calls a JSON-mode
classifier on each, partitions into substantive vs cosmetic, and writes
two markdown files for Ian's review.

Cost estimate: 85 chunks * ~500 tok in + ~50 tok out * $0.15/$0.60 per M = ~$0.01.

This is NOT the locked Stage 2 design. Phase 1 will use Anthropic Haiku
with a properly tuned prompt. See ARCHITECTURE.md §9.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(Path(__file__).parent.parent / ".env")

if not os.environ.get("OPENAI_API_KEY"):
    sys.exit("OPENAI_API_KEY not set. Drop it into .env at repo root.")

client = OpenAI()
MODEL = "gpt-4o-mini"

DIFF_MD = Path(__file__).parent / "pltr_10k_riskdiff.md"
OUT_GATED = Path(__file__).parent / "pltr_10k_riskdiff_gated.md"
OUT_RAW = Path(__file__).parent / "stage2_dryrun_results.json"


class GateDecision(BaseModel):
    substantive: bool = Field(..., description="True if a thoughtful reader would want this flagged.")
    reason: str = Field(..., description="One short sentence justifying the call.")


SYSTEM_PROMPT = """You evaluate whether a textual change between two versions of an SEC 10-K Risk Factors section is SUBSTANTIVE or COSMETIC.

SUBSTANTIVE — would want flagged:
- New risk categories or topics introduced (e.g. generative AI, new geography, regulatory exposure)
- New corporate events mentioned (share repurchase, acquisition, restructuring)
- New products or platforms (e.g. "Artificial Intelligence Platform (AIP)")
- Material change in risk language (e.g. going-concern-adjacent additions, removal of a risk category)
- Genuinely new explanatory paragraph

COSMETIC — would NOT want flagged:
- Pure number updates inside otherwise identical sentences (headcount, percentages, dollar amounts, customer concentration ratios)
- Date rolls
- Citation reformatting, section-break reformatting, page-number changes
- Counsel rewording that preserves the same risk concept (e.g. "recently sued" -> "were sued")
- Structural moves without new content

Return JSON ONLY in the form: {"substantive": true|false, "reason": "<short justification>"}"""


def parse_chunks(md_text: str) -> list[dict]:
    """Parse the chunks out of pltr_10k_riskdiff.md."""
    chunks = []
    # Each chunk starts with "### Chunk N — `tag`"
    pattern = re.compile(r"### Chunk (\d+) — `(\w+)`\s*\n+\| OLD \(FY22\) \| NEW \(FY23\) \|\s*\n\|---\|---\|\s*\n\| (.+?) \| (.+?) \|", re.DOTALL)
    for m in pattern.finditer(md_text):
        chunks.append({
            "n": int(m.group(1)),
            "tag": m.group(2),
            "old": m.group(3).replace("<br>", "\n").replace("\\|", "|").strip(),
            "new": m.group(4).replace("<br>", "\n").replace("\\|", "|").strip(),
        })
    return chunks


def gate_chunk(chunk: dict, retries: int = 2) -> GateDecision:
    user_msg = (
        f"Section: Risk Factors (Item 1A) — change type: {chunk['tag']}\n\n"
        f"OLD (FY22):\n{chunk['old'][:3000]}\n\n"
        f"NEW (FY23):\n{chunk['new'][:3000]}"
    )
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=200,
            )
            raw = resp.choices[0].message.content
            data = json.loads(raw)
            return GateDecision(**data)
        except (ValidationError, json.JSONDecodeError, KeyError) as e:
            if attempt == retries:
                return GateDecision(substantive=False, reason=f"PARSE_ERROR after {retries+1} tries: {e}")
            time.sleep(0.5)
        except Exception as e:
            if attempt == retries:
                return GateDecision(substantive=False, reason=f"API_ERROR after {retries+1} tries: {e}")
            time.sleep(1.0)


def format_chunk_for_md(chunk: dict, decision: GateDecision) -> str:
    old_disp = chunk["old"].replace("\n", "<br>").replace("|", "\\|")
    new_disp = chunk["new"].replace("\n", "<br>").replace("|", "\\|")
    return (
        f"### Chunk {chunk['n']} — `{chunk['tag']}` — gate `{decision.substantive}`\n\n"
        f"**Gate reason:** {decision.reason}\n\n"
        f"| OLD (FY22) | NEW (FY23) |\n"
        f"|---|---|\n"
        f"| {old_disp} | {new_disp} |\n"
    )


def main():
    md_text = DIFF_MD.read_text(encoding="utf-8")
    chunks = parse_chunks(md_text)
    print(f"Loaded {len(chunks)} chunks from {DIFF_MD.name}")
    if not chunks:
        sys.exit("No chunks parsed — check regex against the diff md format.")

    decisions: list[tuple[dict, GateDecision]] = []
    for i, c in enumerate(chunks, 1):
        d = gate_chunk(c)
        decisions.append((c, d))
        flag = "S" if d.substantive else "."
        print(f"  [{i:>3}/{len(chunks)}] chunk {c['n']:>3} ({c['tag']:<8}) -> {flag}  {d.reason[:80]}")

    substantive = [(c, d) for c, d in decisions if d.substantive]
    cosmetic = [(c, d) for c, d in decisions if not d.substantive]
    print(f"\nGated: substantive={len(substantive)}  cosmetic={len(cosmetic)}  total={len(decisions)}")

    # Write gated markdown
    lines: list[str] = []
    lines.append(f"# PLTR FY22 vs FY23 10-K Risk Factors — Stage 2 dry-run gate (OpenAI {MODEL})\n")
    lines.append(f"Generated by `spike/stage2_dryrun.py`. Exploratory only; Phase 1 production uses Anthropic Haiku per `ARCHITECTURE.md` §9.\n")
    lines.append(f"- **Substantive:** {len(substantive)} of {len(chunks)}")
    lines.append(f"- **Cosmetic:** {len(cosmetic)} of {len(chunks)}\n")
    lines.append("## SUBSTANTIVE chunks (Ian: scan these for Stage 2 few-shot positives)\n")
    for c, d in substantive:
        lines.append(format_chunk_for_md(c, d))
    lines.append("\n## COSMETIC chunks (Ian: scan a few for Stage 2 few-shot negatives)\n")
    for c, d in cosmetic:
        lines.append(format_chunk_for_md(c, d))

    OUT_GATED.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_GATED}")

    # Write raw JSON for downstream analysis
    OUT_RAW.write_text(
        json.dumps(
            [{"chunk": c, "decision": d.model_dump()} for c, d in decisions],
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {OUT_RAW}")


if __name__ == "__main__":
    main()
