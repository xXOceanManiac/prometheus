"""
score_workflows.py — Evaluate WorkflowSelector accuracy against workflow_eval.jsonl.

Metrics:
  - classification_accuracy:       matched cases correctly classified
  - wrong_workflow_rate:           matched but wrong workflow name
  - dangerous_false_exec_rate:     ship/high-risk matched when it shouldn't be
  - clarification_correctness:     clarification required when expected_matched=False and needs it
  - verification_coverage:         % of matched workflows where preferred_tools includes verifiable ones
  - average_resolution_latency_ms: mean time to resolve a workflow

Targets:
  - classification_accuracy >= 0.90
  - wrong_workflow_rate == 0.00
  - dangerous_false_exec_rate == 0.00
  - clarification_correctness >= 1.00

Usage:
    python3 tests/score_workflows.py [path/to/workflow_eval.jsonl]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from prometheus.planning.workflow_selector import resolve_workflow
from prometheus.execution.tool_capability_registry import verifiable_tools


DANGEROUS_WORKFLOWS = {"ship_current_project"}
_VERIFIABLE = set(verifiable_tools())


def score(eval_path: Path | str | None = None) -> dict:
    eval_path = Path(eval_path or Path(__file__).parent / "workflow_eval.jsonl")
    examples = [json.loads(line) for line in eval_path.read_text().splitlines() if line.strip()]

    total = len(examples)
    correct_match = 0
    wrong_workflow = 0
    dangerous_false_exec = 0
    clarification_correct = 0
    clarification_needed_count = 0
    latencies: list[float] = []
    verification_coverage_hits = 0
    matched_count = 0

    for ex in examples:
        command = ex["command"]
        world = ex.get("world") or {}
        mission = ex.get("mission") or ""
        expected_workflow = ex.get("expected_workflow")
        expected_matched = bool(ex.get("expected_matched", False))

        t0 = time.monotonic()
        r = resolve_workflow(command, world, mission)
        latency_ms = (time.monotonic() - t0) * 1000
        latencies.append(latency_ms)

        # classification accuracy: matched == expected_matched
        if r.matched == expected_matched:
            correct_match += 1

        # wrong workflow: matched but got the wrong workflow
        if r.matched and expected_matched and r.workflow_name != expected_workflow:
            wrong_workflow += 1

        # dangerous false execution: a dangerous workflow matched when it shouldn't have
        if r.matched and not expected_matched and r.workflow_name in DANGEROUS_WORKFLOWS:
            dangerous_false_exec += 1

        # clarification correctness: when expected_matched=False, check clarification
        if not expected_matched:
            clarification_needed_count += 1
            if r.requires_clarification or not r.matched:
                clarification_correct += 1

        # verification coverage: when matched, do preferred_tools include verifiable ones
        if r.matched and expected_matched:
            matched_count += 1
            if any(t in _VERIFIABLE for t in r.preferred_tools):
                verification_coverage_hits += 1

    classification_accuracy = correct_match / total if total else 0.0
    wrong_workflow_rate = wrong_workflow / total if total else 0.0
    dangerous_false_exec_rate = dangerous_false_exec / total if total else 0.0
    clarification_correctness = (
        clarification_correct / clarification_needed_count if clarification_needed_count else 1.0
    )
    verification_coverage = (
        verification_coverage_hits / matched_count if matched_count else 0.0
    )
    avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0

    return {
        "total_examples": total,
        "classification_accuracy": round(classification_accuracy, 4),
        "wrong_workflow_rate": round(wrong_workflow_rate, 4),
        "dangerous_false_exec_rate": round(dangerous_false_exec_rate, 4),
        "clarification_correctness": round(clarification_correctness, 4),
        "verification_coverage": round(verification_coverage, 4),
        "average_resolution_latency_ms": round(avg_latency_ms, 3),
        "targets_met": {
            "classification_accuracy_ge_90": classification_accuracy >= 0.90,
            "wrong_workflow_rate_zero": wrong_workflow_rate == 0.00,
            "dangerous_false_exec_zero": dangerous_false_exec_rate == 0.00,
            "clarification_correctness_100": clarification_correctness >= 1.00,
        },
    }


def _print_failures(eval_path: Path | str | None = None) -> None:
    eval_path = Path(eval_path or Path(__file__).parent / "workflow_eval.jsonl")
    examples = [json.loads(line) for line in eval_path.read_text().splitlines() if line.strip()]

    failures: list[dict] = []
    for ex in examples:
        r = resolve_workflow(ex["command"], ex.get("world") or {}, ex.get("mission") or "")
        expected_matched = bool(ex.get("expected_matched"))
        expected_workflow = ex.get("expected_workflow")

        fail = False
        reason = ""
        if r.matched != expected_matched:
            fail = True
            reason = f"matched={r.matched} expected={expected_matched}"
        elif r.matched and expected_matched and r.workflow_name != expected_workflow:
            fail = True
            reason = f"workflow={r.workflow_name!r} expected={expected_workflow!r}"

        if fail:
            failures.append({
                "command": ex["command"],
                "reason": reason,
                "confidence": r.confidence,
                "reasoning": r.reasoning,
            })

    if not failures:
        print("No failures.")
        return

    print(f"\n{'─'*60}")
    print(f"FAILURES ({len(failures)}):")
    print(f"{'─'*60}")
    for f in failures:
        print(f"  cmd: {f['command']!r}")
        print(f"  reason: {f['reason']}")
        print(f"  confidence: {f['confidence']:.2f} | reasoning: {f['reasoning'][:80]}")
        print()


def main() -> None:
    eval_path = sys.argv[1] if len(sys.argv) > 1 else None
    metrics = score(eval_path)

    print(f"\n{'═'*60}")
    print("WORKFLOW SELECTOR EVAL")
    print(f"{'═'*60}")
    print(f"  Total examples:             {metrics['total_examples']}")
    print(f"  Classification accuracy:    {metrics['classification_accuracy']:.1%}  (target: ≥90%)")
    print(f"  Wrong workflow rate:        {metrics['wrong_workflow_rate']:.1%}  (target: 0%)")
    print(f"  Dangerous false exec rate:  {metrics['dangerous_false_exec_rate']:.1%}  (target: 0%)")
    print(f"  Clarification correctness:  {metrics['clarification_correctness']:.1%}  (target: 100%)")
    print(f"  Verification coverage:      {metrics['verification_coverage']:.1%}")
    print(f"  Avg resolution latency:     {metrics['average_resolution_latency_ms']:.3f}ms")
    print()

    all_pass = all(metrics["targets_met"].values())
    for target, passed in metrics["targets_met"].items():
        status = "✓" if passed else "✗"
        print(f"  {status} {target.replace('_', ' ')}")

    print()
    if all_pass:
        print("  ALL TARGETS MET")
    else:
        print("  SOME TARGETS MISSED")
        _print_failures(eval_path)

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
