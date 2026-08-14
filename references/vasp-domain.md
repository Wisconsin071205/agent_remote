# VASP domain behavior

## Primary workflow

1. Run `vasp-validate` before submission.
2. Report every `severity=error` issue and do not submit until the user resolves it.
3. Summarize important INCAR choices in calculation terms: relaxation/static, spin, smearing, precision, functional additions, and output retention.
4. Ask for explicit approval immediately before `submit`.
5. Use `jobs` for scheduler state and `vasp-progress` for scientific progress.
6. Use `vasp-inspect` when diagnosing convergence, incomplete runs, or common VASP failures.
7. Treat `completed=true` as program termination, not proof of physical or ionic convergence.
8. Treat `ionic_converged=true` as VASP's structural stopping criterion. Report energy and other available evidence separately.
9. If `electronic_reached_nelm=true`, warn that the last electronic cycle reached `NELM`; do not claim convergence.
10. Never recommend deleting `WAVECAR`, `CHGCAR`, or calculation directories without explicit user intent and a recovery plan.

## Structured inspection schema

The Vlab gateway emits JSON with `schema_version: 1`. Stable top-level fields include:

- `directory`, `mode`
- `files`: existence and size of standard VASP files
- `incar`: selected non-secret calculation parameters
- `structure`: title, species, counts, atom total when parsable
- `potcar_titles`: dataset titles only, never full POTCAR content
- `kpoints_preview`: first five short lines
- `job_scripts`
- `progress`: ionic/electronic steps, final energies, completion/convergence flags, elapsed time, magnetization
- `errors`: recognized VASP error signatures and counts
- `issues`, `warnings`: deterministic preflight and convergence findings

Preserve raw JSON alongside later training/evaluation examples so labels can be regenerated. Do not train on passwords, TOTP material, private keys, full proprietary POTCAR content, or personally identifying paths.

## Scope of current checks

Validation is deliberately conservative and syntactic. It checks required file presence, KPOINTS versus KSPACING, POSCAR/POTCAR dataset counts, common job script names, basic progress, and known error strings. It does not yet prove that ENCUT, k-mesh, pseudopotentials, magnetic initialization, Hubbard U, functional, cell constraints, or convergence thresholds are scientifically appropriate. Those require project-specific policy and benchmark data.
