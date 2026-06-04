# Unused Code Archive

This folder is the conservative holding area for code or notes that are no
longer referenced by the live package, tests, CLI entry points, or the current
beatmap/workbench flows, but are still worth preserving for recovery or
historical context.

## 2026-06-03 repo sweep

Scope:

- keep the beatmap session-creation/conversion path
- keep the workbench/tooling surface for audio/MIDI/session construction
- move only code that is clearly orphaned after a repo-wide reference census

Repo-wide findings:

- `ptxformatwriter.body_synth`, `ptxformatwriter.click_clone`, `ptxformatwriter.wavecache`,
  `ptxformatwriter.beatmap`, `ptxformatwriter.final_index`, `ptxformatwriter.writer`,
  `ptxformatwriter.audit`, `ptxformatwriter.mixed_order`, `ptxformatwriter.midi`, the CLI wrappers,
  and the standalone workbench scripts are all still live enough to keep
- the only clearly dead tracked code found in live modules during this pass was
  two private helpers from `ptxformatwriter/writer.py`
- the old root-level `dead_code.md` note was also moved here because it is
  historical cleanup context, not active library surface

Archived items:

- `removed_writer_helpers_2026-06-03.md`
- `body_synth-dead-code-2026-05-30.md`
