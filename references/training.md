# Training and evaluation data

Treat server operations as tool-grounded VASP reasoning, not as free-form shell prediction.

## Record shape

Store one JSON object per line:

```json
{
  "schema_version": 1,
  "task": "diagnose_vasp",
  "user_request": "判断这个结构优化是否收敛",
  "tool_name": "inspect_vasp_calculation",
  "tool_arguments": {"directory": "<REMOTE_ROOT>/calc-001"},
  "tool_result": {"schema_version": 1, "progress": {}, "issues": [], "errors": []},
  "expert_answer": "基于工具证据的中文回答",
  "labels": {"safe": true, "expert_reviewed": true}
}
```

Use `scripts/training-record.py` to create a redacted record from captured inspection JSON and an expert answer.

## Recommended task families

- preflight validation before submission
- distinguish finished, electronically converged, and ionically converged
- diagnose NELM, BRMIX, ZBRENT, EDDDAV, ZHEGV, POSMAP, and incomplete termination
- compare energies only when calculation settings and structures make comparison meaningful
- choose continuation artifacts without deleting recoverable files
- explain scheduler state separately from scientific progress
- refuse unsafe submissions, destructive actions, and requests for secrets

## Quality rules

- Require tool evidence in every factual server-state example.
- Preserve the structured tool result; do not train only on prose summaries.
- Have a VASP practitioner review scientific claims and corrective actions.
- Split train/evaluation sets by material system or calculation campaign to reduce leakage.
- Include negative examples where information is insufficient and the correct response is to request more evidence.
- Redact user names, absolute home paths, job/account identifiers, hostnames, API keys, passwords, OTPs, private keys, and proprietary full POTCAR content.
- Never use live credentials or unreviewed model output as training labels.
