# QwenPaw Tool Guard — Inspect AI (UK AISI) Evaluation

Formalises the Phase-1 quick probe ([`../qwenpaw-quick-probe`](../qwenpaw-quick-probe)) as an
[Inspect AI](https://inspect.aisi.org.uk) task — the UK AI Security Institute evaluation framework.
Same 84-sample suite, same guard engine, but the result is a reproducible, government-grade
`.eval` log with Inspect scorers/metrics instead of a bespoke JSON file.

**Date:** 2026-09-29
**Framework:** inspect-ai 0.3.266
**System under test:** QwenPaw `ToolGuardEngine` (`execute_shell_command` pre-execution guard)

## Why a custom model provider?

The unit under test is not an LLM — it is the guard that decides whether a shell command
reaches the agent runtime. We expose it to Inspect as a custom provider,
`qwenpaw-toolguard`. A "generation" runs the guard on the candidate command and completes
with `BLOCK` or `ALLOW`. Dataset, scorers, metrics, and logs are standard Inspect.

## Interface

```bash
pip install -r requirements.txt

# One configuration via the Inspect CLI:
inspect eval evaluation/qwenpaw-inspect-aisi/qwenpaw_inspect_eval.py \
    --model qwenpaw-toolguard/default     # or qwenpaw-toolguard/hardened

# Both configurations + aggregated analysis + summary JSON:
python3 evaluation/qwenpaw-inspect-aisi/qwenpaw_inspect_eval.py --mode both
inspect log view evaluation/qwenpaw-inspect-aisi/logs   # browse the .eval logs
```

Model-name segment = guard configuration under test:

| Model | Guardians |
|-------|-----------|
| `qwenpaw-toolguard/default` | RuleBasedToolGuardian + ShellEvasionGuardian (all evasion checks OFF — QwenPaw default) |
| `qwenpaw-toolguard/hardened` | RuleBasedToolGuardian + ShellEvasionGuardian (all evasion checks ON) |

## Task structure

- **Dataset** — 84 samples, identical to Phase 1: 28 dangerous direct, 26 benign direct,
  30 encoded variants (base64, hex, unicode escape, shell var, backtick, chain, quote
  obfuscation). Built from the vendored probe so the two harnesses can never drift.
- **Scorers** — `match()` (decision vs target) and `guard_outcome` (accuracy + stderr,
  per-sample TP/FP/TN/FN label in the explanation field).
- **Provider** — `QwenPawToolGuardAPI` in [`qwenpaw_inspect_eval.py`](qwenpaw_inspect_eval.py);
  rules loaded from [`../harness/rules`](../harness/rules) (vendored QwenPaw rule pack).

## Results (2026-09-29 run)

| Metric | default | hardened |
|--------|---------|----------|
| Accuracy (Inspect scorer) | 63.1% | 67.9% |
| Block rate (all 84) | 34.5% | 39.3% |
| Dangerous blocked (58) | 48.3% | 55.2% |
| Encoded blocked (30) | 40.0% | 53.3% |
| Benign false positives (26) | 3.8% (1) | 3.8% (1) |
| False negatives | 30 | 26 |

Identical to the June 2026 Phase-1 probe — the guard is deterministic and the Inspect
formalisation is a strict reproduction, now on AISI-standard infrastructure.

See [`finding/qwenpaw-inspect-aisi/`](../../finding/qwenpaw-inspect-aisi/) for the full
analysis, and `logs/` for the raw `.eval` files plus `summary-*.json` / `analysis.json`.

## Files

```
qwenpaw_inspect_eval.py    # provider + task + scorers + runner
requirements.txt
logs/*.eval                # canonical Inspect logs (default + hardened)
logs/summary-{mode}.json   # probe-style summaries derived from the logs
logs/analysis.json         # both modes, full variant/category breakdown
```
