# Audit Package

This package will own evidence references, rollback maps, human-readable reports, and artifact lineage utilities.

Current scaffold responsibilities:

- Keep audit concepts separate from UI and worker orchestration.
- Provide the target location for `InferenceRecord`, `ReviewRun`, runtime evidence, and report builders.
- Preserve the product rule that every AI inference, deterministic transform, tool call, browser validation, and repair action is traceable.

