# Local Codex Skill directory

This directory is discovered by Codex as local assistance Skills. It is not a
runtime business-Skill loading path and must never be treated as a business
design source.

The sole design and implementation authority for this repository is
`docs/EFFECTIVE_DESIGN_BASELINE.md`. Runtime business Skills live under
`runtime_skills/` and are invoked only through the controlled Core/Model Port
path.

Only reusable, non-workflow-wrapper Skills remain here. They may help with
writing hygiene, domain research context, outlines, or project review, but
must not introduce a competing workflow, read archived materials as design
authority, or direct a model to retired scripts.
