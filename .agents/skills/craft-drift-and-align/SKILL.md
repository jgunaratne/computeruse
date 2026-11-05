---
name: craft-drift-and-align
description: "Audit how far computeruse has drifted from the Craft Design System and from its own design doc, repair whichever side drifted (the code, or the docs/manifest), ratchet the baseline, and report. Use after a big feature or a run of quick fixes, before a release, after a kit upgrade, when a rule turned out to be wrong, or when asked "is the UI still on the design system?"."
---

# Drift and align (Craft DS)

Four phases: **audit** what is real → **align the harness** → **conform the code** → **report**. The harness here is `craft-ds.json`, `docs/DESIGN.md`, `docs/DECISIONS.md`, `docs/LESSONS.md`, the `craft-ds` rule, the skills, the reviewer, and the hook files under `.claude/` and `.agents/`.

Two kinds of drift, two repairs:
- **The harness drifted** (code is right; the doc, manifest or rule is stale, missing or dead) → phase 2.
- **The code drifted** (the rule is right; the code breaks it) → phase 3.

## Phase 0: baseline (read-only)

1. `git status --short`, `git log --oneline -5`: note a dirty tree.
2. `~/git/craft-ds/bin/craft-ds check .`: the gate (manifest, kit pins, docs that point at nothing, frontmatter, mirrors, hooks). Red here is fixed first.
3. `~/git/craft-ds/bin/craft-ds probe .`: all probes; keep the counts, they are the "before". `kit-intact` red means someone edited the kit: `~/git/craft-ds/bin/craft-ds update .` restores it (and note what they wanted, it may be a real gap).
4. The project's own tests, lint and typecheck, if it has them.

## Phase 1: audit (read-only)

1. Compare every claim in `docs/DESIGN.md` and the rule against the code: paths, class names, token names, theme ids, widths, behaviours, "always/never" sentences.
2. Look the other way: what does the code do that no doc mentions? New screens, components, project tokens, grammar-specific selectors, a theme list that grew, copy patterns, recent fixes that encode a lesson.
3. Compare against the kit: does the project still look like the showcase (`web/public/vendor/craft-ds/index.html`) in `proMedium` (Pro (Medium Gray), pro grammar), `pro` (Pro (Dark), pro grammar), `proLight` (Pro (Light), pro grammar), `studio` (Studio (Dark), studio grammar), `studioLight` (Studio (Light), studio grammar), `classic` (Classic, classic grammar)? Shoot with `craft-visual-check` and read the PNGs.
4. Classify every finding:

   | Class | Meaning | Repair |
   |---|---|---|
   | `DOC-STALE` | The code is right; the design doc, rule or manifest says something else | Phase 2 |
   | `GAP` | A real pattern or decision no doc records; UI files no rule covers | Phase 2 |
   | `DEAD` | The harness describes something that no longer exists | Phase 2 |
   | `CODE-DRIFT` | The rule is right; the code breaks it (a probe finding, or a picture) | Phase 3 |
   | `KIT-GAP` | The project needs something the kit should provide | Propose upstream in `~/git/craft-ds`; meanwhile compose locally and record it |
   | `CONFLICT` | Docs disagree, or it is unclear who is right | Ask |

5. Who is right, in this order: the user's latest instruction → an accepted entry in `docs/DECISIONS.md` → behaviour pinned by a passing test → the kit (`web/public/vendor/craft-ds/tokens/tokens.json`, `web/public/vendor/craft-ds/css/craft-ds.css`, `web/public/vendor/craft-ds/DESIGN.md`) → the rule. Still unsure → `CONFLICT`.

### Checkpoint
Stop and ask before phases 2–3 if any finding is `CONFLICT`, or a repair would change user-visible behaviour or copy, delete code or docs, add a dependency, or touch the kit. Everything else proceeds.

## Phase 2: align the harness

1. Fix `DOC-STALE` claims; remove `DEAD` ones.
2. Close `GAP`s where they belong, once: a cross-cutting rule → the project's conventions file / `AGENTS.md` (or the rule body, then `~/git/craft-ds/bin/craft-ds apply . --force --only rules` keeps both tools in step); a choice someone could re-litigate → a numbered entry in `docs/DECISIONS.md`; a trap → `docs/LESSONS.md`; a repeated workflow → a skill step; a new review concern → `craft-design-reviewer`.
3. Manifest: `sources`/`styles` cover every UI file (`rule-coverage`), `themes` still cover all three grammars, `allow_raw` lists only real brand art, `enforce` matches what the project promised.
4. Ratchet: when a rule can be checked mechanically and the probes don't, say so in the report (and propose the probe upstream in `~/git/craft-ds`).
5. `~/git/craft-ds/bin/craft-ds check .` is green.

## Phase 3: conform the code

1. Fix `CODE-DRIFT` probe by probe, smallest behaviour-preserving change first: literals → tokens, `[data-craft-theme=…]` → tokens or grammar flags, `.craft-*` restyles → a project class, unknown classes/icons → real ones, unnamed icon buttons → `aria-label`.
2. Anything a picture caught (clipping at the narrow width, a theme that doesn't apply, a Classic bevel missing): fix, reshoot, reread.
3. Respect the guard: the kit is never edited; `~/git/craft-ds/bin/craft-ds update .` if it was.

## Phase 4: verify and report

1. `~/git/craft-ds/bin/craft-ds check .` and `~/git/craft-ds/bin/craft-ds probe .` again: the "after". If findings remain and are accepted, `~/git/craft-ds/bin/craft-ds probe . --update-baseline` and say why.
2. `craft-visual-check` on what changed; `craft-design-reviewer` for the UI diff.
3. Don't commit unless asked.

## Report

```
## Drift and align (computeruse): <date>

**Before:** check <ok|n problems> · probes <code n / harness n>
**After:**  check ok · probes <code n / harness n> · baseline <unchanged|updated: why>

### Findings (<n>)
| # | Class | Where | Finding | Who's right (why) | Resolution |
|---|---|---|---|---|---|

### Harness changes
- <file>: <what and why>

### Code changes
- <file>: <what>, verified by <probe / screenshot>

### Upstream (KIT-GAP)
- <what the kit should grow>

### Needs your decision
- <question> · recommendation: <…>
```
Counts come from real output; unfixed findings are listed as unfixed.
