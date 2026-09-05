# Repository architecture diagrams

Generated with the installed [tt-a1i/archify](https://github.com/tt-a1i/archify) skill (v2.16). No application code or tests were edited.

- [High-level architecture](high-level.html): nine architectural groups and the main control paths.
- [Complete component map](detailed.html): all 273 snapshot files in 31 ownership regions, plus the eight enforced import layers. Use search, pan and zoom.
- [Component inspector](inventory.html): 3,176 Python and TypeScript/JavaScript declarations, source line numbers, and 540 resolved Python import relationships.
- [Machine-readable inventory](inventory.json): paths, hashes, symbols and imports.

## Scope and interpretation

The snapshot includes tracked and non-ignored working-tree files, including the pre-existing local edits and notes. It excludes Git internals, ignored build products, environments, run directories and these newly generated outputs. Each file has exactly one node in the complete map. Package initializer files, tests, stylesheets, contact-card fixtures, lockfiles, installers, CI, configuration and documentation are included.

The complete map is a containment architecture, not a 540-arrow call graph. Its top rail describes permitted downward imports, including skipped layers. Two deliberate deferred-import exceptions are documented in pyproject.toml: workspace → session and subagents → config. settings.py and mcp_demo are shown in the file inventory but are not additional enforced layers. The inspector resolves Python imports, including deferred and TYPE_CHECKING imports; it does not interpret them as runtime calls. TypeScript declaration extraction is lexical, and non-Python dependency resolution is not claimed.

The high-level map groups shared capabilities. Batch and REPL differ: batch uses a copied workspace and independent verifier; REPL uses the operator directory and terminates when tool requests stop. Model requests and action execution repeat; the arrows show principal interactions, not every observation, recovery or failure edge. Repository support is a supporting component group rather than an online service. Local sandbox execution is not a kernel security boundary.

## Validation and review

Both HTML artifacts were committed by Archify deliver with matching specification/artifact hashes. The high-level diagram passes showcase validation and automated browser checks. The dense map passes standard artifact checks with a projected-text readability warning. It fits all checked desktop viewports but fails the browser readability threshold at overview scale; zoom is necessary. Do not interpret standard delivery as showcase acceptance.

Screenshots were inspected in both light and dark themes. High-level visual review passed. Dense-map visual review fails first-screen label readability; this is disclosed rather than hidden by clipping or reducing fonts. Two visual correction rounds converted the tall file list into a compact poster and added the import-layer rail. The supplementary inventory passed JavaScript syntax checking; opening it through the in-app browser was blocked by the browser URL policy, so interactive browser QA is not claimed.

Viewport evidence: 1440×900, 1600×1000, 1920×1080 and 2048×1320. See the artifact-bound *.visual-check.json receipts and screenshot contact sheets.

## Artifact receipts

### high-level

```text
diagram_type: architecture
output: /Users/moinuddin/Documents/AIYatra/Harness-Engineering-2/yatra-harness/docs/architecture-diagrams/high-level.html
specification_sha256: 923d7d51820f8607ea2b4d86bcda31bde7a83577ea483ed8553f76b475902c6b
artifact_sha256: a6fbbb1459350a33d46de2fab4c282629d9735a50eb32f4fb67d93dd71af7efa
validation: 9/9 showcase; 0 errors; 0 warnings
browser_evidence: passed
visual_review: passed
correction_rounds: 0
```

### detailed

```text
diagram_type: architecture
output: /Users/moinuddin/Documents/AIYatra/Harness-Engineering-2/yatra-harness/docs/architecture-diagrams/detailed.html
specification_sha256: dcbb84b018593800623abc7b266ce52e482c8da7174b28168c6568ff12ba0d6b
artifact_sha256: 9f5e1821711fbc1b394f03a870b9f400c9c0e78f8d6fdc91e5a3ee7aa06235d6
validation: 9/9 standard; 0 errors; 1 readability warning
browser_evidence: failed
visual_review: failed (overview text requires zoom)
correction_rounds: 2
```

## Re-rendering

The JSON files are the editable Archify sources. Run validate, deliver and visual-check using the installed skill CLI. Do not edit the delivered HTML directly. Keep the detailed map on the standard quality profile because its exhaustive scope intentionally exceeds first-screen text density.

## Repository checks

See [verification.txt](verification.txt) for the final repository check outcome.
