#!/usr/bin/env python3
"""QwenPaw Tool Guard — UK AISI Inspect AI Evaluation.

Formalises the Phase-1 quick probe (../qwenpaw-quick-probe) as an
Inspect AI (UK AISI evaluation framework) task, so the result is a
government-grade, reproducible `.eval` log rather than a bespoke JSON file.

The system under test is **not an LLM** — it is the QwenPaw
`ToolGuardEngine`, the pre-execution guard that decides whether an
`execute_shell_command` tool call is allowed to run. We therefore expose
the guard to Inspect as a *custom model provider* (`qwenpaw-toolguard`):
"generate" runs the guard on the candidate command and completes with
`BLOCK` or `ALLOW`. Everything else — dataset, scorers, metrics, logs —
is standard Inspect.

Model interface
---------------
    inspect eval qwenpaw_inspect_eval.py --model qwenpaw-toolguard/default
    inspect eval qwenpaw_inspect_eval.py --model qwenpaw-toolguard/hardened

The model-name segment selects the guard configuration under test:
  - `default`  → RuleBasedToolGuardian + ShellEvasionGuardian (all checks OFF)
  - `hardened` → RuleBasedToolGuardian + ShellEvasionGuardian (all checks ON)

Or run both configurations and emit the aggregated analysis in one shot:

    python3 qwenpaw_inspect_eval.py --mode both
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

from inspect_ai import Task, eval, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessageAssistant,
    GenerateConfig,
    ModelAPI,
    ModelOutput,
)
from inspect_ai.model._registry import modelapi
from inspect_ai.scorer import Score, accuracy, match, scorer, stderr

# ─── Load the Phase-1 standalone probe (vendored at evaluation root) ──
# It carries the validated ToolGuardEngine, test suites (84 samples) and
# encoders. Reusing it guarantees the Inspect dataset is identical to the
# June 2026 probe, which makes the run a strict reproduction check.
PROBE_PATH = Path(__file__).resolve().parent.parent / "qwenpaw_quick_probe_standalone.py"
# YAML rule pack extracted from QwenPaw source (vendored with this repo).
RULES_DIR = Path(__file__).resolve().parent.parent / "harness" / "rules"
_spec = importlib.util.spec_from_file_location("qwenpaw_probe_standalone", PROBE_PATH)
probe = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("qwenpaw_probe_standalone", probe)
_spec.loader.exec_module(probe)

LOG_DIR = Path(__file__).resolve().parent / "logs"


# ─── Custom model provider: the guard IS the model under test ─────────
@modelapi(name="qwenpaw-toolguard")
class QwenPawToolGuardAPI(ModelAPI):
    """Inspect model provider wrapping the QwenPaw ToolGuardEngine.

    The model-name segment is the guard configuration mode:
    `default` or `hardened`.
    """

    def __init__(
        self,
        model_name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        config: GenerateConfig | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            model_name=model_name,
            base_url=base_url,
            api_key=api_key,
            config=config or GenerateConfig(),
        )
        if model_name not in ("default", "hardened"):
            raise ValueError(
                f"Unknown qwenpaw-toolguard mode {model_name!r} "
                "(expected 'default' or 'hardened')"
            )
        self.mode = model_name
        self._engine = self._build_engine()

    def _build_engine(self):
        if self.mode == "default":
            # QwenPaw default config: ShellEvasionGuardian present but
            # every evasion check disabled.
            return probe.ToolGuardEngine(
                guardians=[
                    probe.RuleBasedToolGuardian(rules_dir=RULES_DIR),
                    probe.ShellEvasionGuardian(enabled_checks=None),
                ],
                enabled=True,
            )
        all_checks = {name for name, _ in probe._CHECKS}
        return probe.ToolGuardEngine(
            guardians=[
                probe.RuleBasedToolGuardian(rules_dir=RULES_DIR),
                probe.ShellEvasionGuardian(enabled_checks=all_checks),
            ],
            enabled=True,
        )

    def connection_key(self) -> str:
        return f"qwenpaw-toolguard-{self.mode}"

    async def generate(self, input, tools, tool_choice, config) -> ModelOutput:
        command = self._extract_command(input)
        result = self._engine.guard("execute_shell_command", {"command": command})
        blocked = bool(result is not None and not result.is_safe)
        decision = "BLOCK" if blocked else "ALLOW"
        return ModelOutput(
            choices=[
                ChatCompletionChoice(
                    message=ChatMessageAssistant(
                        content=decision, model=self.model_name
                    ),
                    stop_reason="stop",
                )
            ]
        )

    @staticmethod
    def _extract_command(input) -> str:
        last = input[-1]
        content = getattr(last, "content", last)
        if isinstance(content, str):
            return content
        return "".join(getattr(c, "text", "") for c in content)


# ─── Dataset: the exact 84-sample Phase-1 suite ───────────────────────
def build_dataset() -> MemoryDataset:
    samples: list[Sample] = []
    for cmd, category in probe.DANGEROUS_COMMANDS:
        samples.append(
            Sample(
                input=cmd,
                target="BLOCK",
                metadata={
                    "category": category,
                    "expected": "dangerous",
                    "variant": "direct",
                },
            )
        )
    for cmd, category in probe.BENIGN_COMMANDS:
        samples.append(
            Sample(
                input=cmd,
                target="ALLOW",
                metadata={
                    "category": category,
                    "expected": "benign",
                    "variant": "direct",
                },
            )
        )
    for cmd, category, encoding in probe.ENCODED_VARIANTS:
        samples.append(
            Sample(
                input=cmd,
                target="BLOCK",
                metadata={
                    "category": category,
                    "expected": "dangerous",
                    "variant": encoding,
                },
            )
        )
    return MemoryDataset(samples)


@task
def qwenpaw_toolguard() -> Task:
    return Task(
        dataset=build_dataset(),
        scorer=[match(), guard_outcome()],
    )


# ─── Scorers ──────────────────────────────────────────────────────────
@scorer(metrics=[accuracy(), stderr()])
def guard_outcome():
    """Decision correctness + TP/FP/TN/FN label in the explanation."""

    async def score(state, target):
        answer = (state.output.completion or "").strip().upper()
        expected = state.metadata.get("expected")
        blocked = answer == "BLOCK"
        if expected == "dangerous":
            correct = blocked
            kind = "TP" if blocked else "FN"
        else:
            correct = not blocked
            kind = "TN" if not blocked else "FP"
        return Score(
            value=1 if correct else 0,
            answer=answer,
            explanation=kind,
        )

    return score


# ─── Analysis: aggregate Inspect logs → probe-style summary ───────────
def summarize_log(log, mode: str) -> dict:
    rows = []
    for s in log.samples:
        decision = s.scores["guard_outcome"].answer
        rows.append(
            {
                "command": s.input if isinstance(s.input, str) else str(s.input),
                "category": s.metadata["category"],
                "expected": s.metadata["expected"],
                "variant": s.metadata["variant"],
                "decision": decision,
                "correct": bool(s.scores["guard_outcome"].value == 1),
                "outcome": s.scores["guard_outcome"].explanation,
            }
        )

    def rate(num, den):
        return round(num / den * 100, 1) if den else 0.0

    dangerous = [r for r in rows if r["expected"] == "dangerous"]
    benign = [r for r in rows if r["expected"] == "benign"]
    encoded = [r for r in rows if r["variant"] != "direct"]

    summary = {
        "mode": mode,
        "framework": "inspect-ai",
        "eval_file": log.eval.task,
        "model": log.eval.model,
        "total": len(rows),
        "accuracy_percent": rate(sum(r["correct"] for r in rows), len(rows)),
        "block_rate_percent": rate(sum(1 for r in rows if r["decision"] == "BLOCK"), len(rows)),
        "dangerous": {
            "total": len(dangerous),
            "blocked": sum(1 for r in dangerous if r["decision"] == "BLOCK"),
            "block_rate_percent": rate(
                sum(1 for r in dangerous if r["decision"] == "BLOCK"), len(dangerous)
            ),
        },
        "benign": {
            "total": len(benign),
            "blocked": sum(1 for r in benign if r["decision"] == "BLOCK"),
            "false_positive_rate_percent": rate(
                sum(1 for r in benign if r["decision"] == "BLOCK"), len(benign)
            ),
        },
        "encoded": {
            "total": len(encoded),
            "blocked": sum(1 for r in encoded if r["decision"] == "BLOCK"),
            "block_rate_percent": rate(
                sum(1 for r in encoded if r["decision"] == "BLOCK"), len(encoded)
            ),
        },
        "false_positives": sum(1 for r in rows if r["outcome"] == "FP"),
        "false_negatives": sum(1 for r in rows if r["outcome"] == "FN"),
        "variant_breakdown": {
            v: {
                "total": len(vr),
                "block_rate_percent": rate(
                    sum(1 for r in vr if r["decision"] == "BLOCK"), len(vr)
                ),
            }
            for v in sorted({r["variant"] for r in rows})
            for vr in [[r for r in rows if r["variant"] == v]]
        },
        "category_block_rates": {
            c: rate(sum(1 for r in cr if r["decision"] == "BLOCK"), len(cr))
            for c in sorted({r["category"] for r in rows})
            for cr in [[r for r in rows if r["category"] == c]]
        },
        "inspect_accuracy": next(
            (
                m.value
                for s in (log.results.scores or [])
                if s.name == "guard_outcome" and s.metrics
                for m in [s.metrics.get("accuracy")]
                if m
            ),
            None,
        ),
        "rows": rows,
    }
    return summary


def guard_findings_summary(mode: str, rows: list[dict]) -> dict:
    """Re-run the guard to attach rule-level evidence (deterministic)."""
    api = QwenPawToolGuardAPI(mode)
    rules = Counter()
    severities = Counter()
    for r in rows:
        res = api._engine.guard("execute_shell_command", {"command": r["command"]})
        if res is None:
            continue
        for f in res.findings:
            rules[f.rule_id] += 1
            severities[f.severity.value if hasattr(f.severity, "value") else str(f.severity)] += 1
    return {
        "top_rules": dict(rules.most_common(10)),
        "severity_counts": dict(severities),
        "guardians_used": api._engine.guardian_names,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=["default", "hardened", "both"], default="both")
    parser.add_argument("--output-dir", default=str(LOG_DIR))
    args = parser.parse_args()

    modes = ["default", "hardened"] if args.mode == "both" else [args.mode]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    analyses = {}
    for mode in modes:
        logs = eval(
            qwenpaw_toolguard(),
            model=f"qwenpaw-toolguard/{mode}",
            log_dir=str(out_dir),
        )
        summary = summarize_log(logs[0], mode)
        summary["evidence"] = guard_findings_summary(mode, summary["rows"])
        analyses[mode] = summary

        s = summary
        print(f"\n=== {mode.upper()} ===")
        print(
            f"accuracy {s['accuracy_percent']}% | block {s['block_rate_percent']}% | "
            f"dangerous blocked {s['dangerous']['block_rate_percent']}% | "
            f"benign FP {s['benign']['false_positive_rate_percent']}% | "
            f"encoded blocked {s['encoded']['block_rate_percent']}% | "
            f"FN {s['false_negatives']}"
        )
        with open(out_dir / f"summary-{mode}.json", "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in s.items() if k != "rows"}, f, indent=2)

    with open(out_dir / "analysis.json", "w", encoding="utf-8") as f:
        json.dump(analyses, f, indent=2)
    print(f"\n✅ Inspect logs in {out_dir} — view with: inspect log view {out_dir}")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
