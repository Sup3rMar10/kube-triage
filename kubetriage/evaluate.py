"""Score the rules against labelled fixtures, separately for a dev split and a holdout split.

The dev split is what the rules were written and adjusted against. The holdout split was written (or captured
from a real cluster) after the rules were frozen. A fixture's expected list is `[{subject, rule, variant?}]`:
which pod or node should be flagged by which rule, and optionally which sub-type.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path

from . import __version__
from .engine import diagnose
from .rules import RULE_INFO

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURES = ROOT / "fixtures"
SPLITS = ("dev", "holdout")


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def _prf(tp, fp, fn):
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None}


def load_manifest(fixtures_dir=DEFAULT_FIXTURES) -> list:
    return json.loads((Path(fixtures_dir) / "manifest.json").read_text(encoding="utf-8"))["fixtures"]


def score_fixture(doc, expected) -> dict:
    """Compare findings with expectations for one fixture."""
    report = diagnose(doc, min_severity="info")
    predicted = {}
    for f in report.findings:
        predicted[(f.subject, f.rule)] = f.variant
    want = {(e["subject"], e["rule"]): e.get("variant") for e in expected}
    return {"predicted": predicted, "expected": want}


def evaluate(fixtures_dir=DEFAULT_FIXTURES, splits=SPLITS) -> dict:
    fixtures_dir = Path(fixtures_dir)
    entries = [e for e in load_manifest(fixtures_dir) if e["split"] in splits]
    out = {"kubetriage_version": __version__, "fixtures": {}, "splits": {}, "mismatches": {}}
    for split in splits:
        subset = [e for e in entries if e["split"] == split]
        counts = defaultdict(lambda: [0, 0, 0])      # rule -> tp, fp, fn
        var_total = var_ok = exact = 0
        mismatches = []
        for e in subset:
            doc = json.loads((fixtures_dir / e["file"]).read_text(encoding="utf-8"))
            s = score_fixture(doc, e["expected"])
            pred, want = s["predicted"], s["expected"]
            for key in set(pred) | set(want):
                rule = key[1]
                if key in pred and key in want:
                    counts[rule][0] += 1
                    if want[key] is not None:
                        var_total += 1
                        var_ok += int(want[key] == pred[key])
                        if want[key] != pred[key]:
                            mismatches.append({"fixture": e["name"], "kind": "wrong-variant", "subject": key[0],
                                               "rule": rule, "expected": want[key], "got": pred[key]})
                elif key in pred:
                    counts[rule][1] += 1
                    mismatches.append({"fixture": e["name"], "kind": "false-positive", "subject": key[0],
                                       "rule": rule, "got": pred[key]})
                else:
                    counts[rule][2] += 1
                    mismatches.append({"fixture": e["name"], "kind": "missed", "subject": key[0], "rule": rule,
                                       "expected": want[key]})
            exact += int(set(pred) == set(want))
        tp = sum(c[0] for c in counts.values())
        fp = sum(c[1] for c in counts.values())
        fn = sum(c[2] for c in counts.values())
        overall = _prf(tp, fp, fn)
        overall["precision_ci95"] = wilson(tp, tp + fp)
        overall["recall_ci95"] = wilson(tp, tp + fn)
        out["fixtures"][split] = {"count": len(subset),
                                  "hand": sum(1 for e in subset if e["source"] == "hand"),
                                  "kind": sum(1 for e in subset if e["source"] == "kind")}
        out["splits"][split] = {
            "overall": overall,
            "per_rule": {r: _prf(*counts[r]) for r in sorted(counts)},
            "variant_accuracy": {"correct": var_ok, "total": var_total,
                                 "accuracy": round(var_ok / var_total, 3) if var_total else None},
            "fixtures_exact": {"exact": exact, "total": len(subset)},
        }
        out["mismatches"][split] = mismatches
    return out


def _fmt(v):
    return "n/a" if v is None else f"{v:.2f}"


def render_markdown(summary: dict) -> str:
    sp = summary["splits"]
    lines = []
    fx = summary["fixtures"]
    lines.append("| Split | Fixtures | Hand-built | Captured from kind | Precision | Recall | Exact-match fixtures | Variant accuracy |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for split in summary["splits"]:
        o = sp[split]["overall"]
        va = sp[split]["variant_accuracy"]
        ex = sp[split]["fixtures_exact"]
        lines.append(f"| {split} | {fx[split]['count']} | {fx[split]['hand']} | {fx[split]['kind']} | "
                     f"{_fmt(o['precision'])} ({o['tp']}/{o['tp'] + o['fp']}) | {_fmt(o['recall'])} ({o['tp']}/{o['tp'] + o['fn']}) | "
                     f"{ex['exact']}/{ex['total']} | {va['correct']}/{va['total']} |")
    lines.append("")
    rules = sorted({r for s in sp.values() for r in s["per_rule"]})
    lines.append("| Rule | " + " | ".join(f"{s} P / R (n)" for s in sp) + " |")
    lines.append("|---|" + "---|" * len(sp))
    for r in rules:
        cells = []
        for s in sp:
            c = sp[s]["per_rule"].get(r)
            cells.append("-" if c is None else f"{_fmt(c['precision'])} / {_fmt(c['recall'])} ({c['tp'] + c['fn']})")
        lines.append(f"| {r} {RULE_INFO[r][0]} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def update_readme(readme: Path, markdown: str) -> bool:
    text = readme.read_text(encoding="utf-8")
    new, n = re.subn(r"(<!-- RESULTS:START -->).*?(<!-- RESULTS:END -->)",
                     lambda m: f"{m.group(1)}\n\n{markdown}\n\n{m.group(2)}", text, flags=re.S)
    if n:
        readme.write_text(new, encoding="utf-8")
    return bool(n)
