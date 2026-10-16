"""Command line: diagnose, eval, rules, demo."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .engine import diagnose
from .evaluate import DEFAULT_FIXTURES, ROOT, evaluate, render_markdown, update_readme
from .explain import AnthropicExplainer, explain
from .findings import SEVERITIES, SEVERITY_RANK
from .kubectl import KubectlError, fetch
from .render import render_text
from .rules import RULE_INFO


def _load_document(args):
    if args.file:
        raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        return json.loads(raw), []
    return fetch(namespace=args.namespace, all_namespaces=args.all_namespaces, kubectl=args.kubectl,
                 context=args.context)


def cmd_diagnose(args) -> int:
    try:
        doc, warnings = _load_document(args)
    except (KubectlError, OSError, json.JSONDecodeError) as e:
        print(f"kube-triage: {e}", file=sys.stderr)
        return 3
    for w in warnings:
        print(f"kube-triage: warning: {w}", file=sys.stderr)
    report = diagnose(doc, min_severity=args.min_severity)
    d = report.to_dict()
    explanation = explain(d, AnthropicExplainer()) if args.explain == "anthropic" else ""
    if args.json:
        if explanation:
            d["explanation"] = explanation
        print(json.dumps(d, indent=2))
    else:
        sys.stdout.write(render_text(report))
        if explanation:
            print("\nPlain-English summary (model-generated from the findings above; check it against them):\n")
            print(explanation)
    if args.fail_on and any(SEVERITY_RANK[f.severity] <= SEVERITY_RANK[args.fail_on] for f in report.findings):
        return 1
    return 0


def cmd_eval(args) -> int:
    summary = evaluate(args.fixtures)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    md = render_markdown(summary)
    print(md)
    for split, items in summary["mismatches"].items():
        for m in items:
            print(f"[{split}] {m['kind']}: {m['fixture']} {m['subject']} {m['rule']} "
                  f"expected={m.get('expected')} got={m.get('got')}")
    if args.write_readme and update_readme(ROOT / "README.md", md):
        print(f"updated README results block", file=sys.stderr)
    print(f"wrote {out}", file=sys.stderr)
    return 0


def cmd_rules(_args) -> int:
    for rid, (name, sev, desc) in RULE_INFO.items():
        print(f"{rid}  {sev:<7} {name:<20} {desc}")
    return 0


def cmd_demo(_args) -> int:
    sample = ROOT / "sample" / "shop.json"
    print(f"$ python -m kubetriage diagnose --file sample/shop.json\n")
    sys.stdout.write(render_text(diagnose(json.loads(sample.read_text(encoding="utf-8")))))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kubetriage", description="Explain why Kubernetes workloads are unhealthy.")
    p.add_argument("--version", action="version", version=f"kube-triage {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diagnose", help="diagnose a saved JSON file or a live namespace")
    src = d.add_mutually_exclusive_group()
    src.add_argument("--file", help="kubectl JSON (a List of pods, events and nodes); '-' reads stdin")
    src.add_argument("--namespace", "-n", help="live: read this namespace with kubectl (default: current context's)")
    src.add_argument("--all-namespaces", "-A", action="store_true", help="live: read every namespace")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument("--min-severity", choices=SEVERITIES, default="low", help="hide findings below this (default: low)")
    d.add_argument("--fail-on", choices=SEVERITIES, help="exit 1 if any finding is at least this severe")
    d.add_argument("--explain", choices=["anthropic"], help="have a model rephrase the findings (off by default)")
    d.add_argument("--kubectl", default="kubectl", help="kubectl binary")
    d.add_argument("--context", help="kubectl context")
    d.set_defaults(fn=cmd_diagnose)

    e = sub.add_parser("eval", help="score the rules on fixtures/ (dev and holdout)")
    e.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    e.add_argument("--out", default=str(ROOT / "results" / "summary.json"))
    e.add_argument("--write-readme", action="store_true", help="refresh the results block in README.md")
    e.set_defaults(fn=cmd_eval)

    sub.add_parser("rules", help="list the rules").set_defaults(fn=cmd_rules)
    sub.add_parser("demo", help="diagnose the bundled sample namespace").set_defaults(fn=cmd_demo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "diagnose" and not (args.file or args.namespace or args.all_namespaces):
        args.namespace = None  # current context's default namespace
    return args.fn(args)
