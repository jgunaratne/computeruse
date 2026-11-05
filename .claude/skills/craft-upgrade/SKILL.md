---
name: craft-upgrade
description: "Upgrade the vendored Craft Design System kit in computeruse to the version in the craft-ds checkout, re-pin it, re-run the probes, reshoot every grammar and record what changed. Use when the kit gained tokens, components or themes, when kit-intact reports a hand-edited kit, or when asked to "update the design system"."
---

# Upgrade the kit

The kit under `web/public/vendor/craft-ds/` is never edited in place. It is replaced whole from `~/git/craft-ds` and re-pinned.

## 1. Before

```sh
~/git/craft-ds/bin/craft-ds check .
~/git/craft-ds/bin/craft-ds probe . --json > /tmp/craft-before.json
```
Note the kit version (`craft-ds.json` → `kit.version`) and the probe counts.

## 2. Upgrade

```sh
~/git/craft-ds/bin/craft-ds update .
```
This copies `css/`, `js/`, `tokens/`, `assets/` and the licences from the checkout, rewrites `kit.hashes` and `kit.version`, and prints the version change. If `kit-intact` had flagged a hand edit, read `git diff web/public/vendor/craft-ds` first: whoever edited it wanted something; keep that want as a project style or a `KIT-GAP` note, not as a kit patch.

## 3. After

```sh
~/git/craft-ds/bin/craft-ds check .
~/git/craft-ds/bin/craft-ds probe .
```
New probes or token renames can add findings; fix them in project files. Then `craft-visual-check` on the main screens in `proMedium` (Pro (Medium Gray), pro grammar), `pro` (Pro (Dark), pro grammar), `proLight` (Pro (Light), pro grammar), `studio` (Studio (Dark), studio grammar), `studioLight` (Studio (Light), studio grammar), `classic` (Classic, classic grammar) at 1440, 820 px, and read the PNGs: a kit upgrade is a visual change.

## 4. Record

- `docs/DECISIONS.md`: one numbered entry if the upgrade changed a decision (a token the project used was renamed, a component the project composed by hand now exists in the kit).
- `docs/LESSONS.md`: anything that surprised you.
- Commit the kit folder and `craft-ds.json` together, so the pins always match the files.

## Report

```
Kit upgrade: v<old> → v<new>
- files refreshed: <n>
- probes: before <code/harness> → after <code/harness>; fixed: …
- shots read: <n>; visual changes noticed: …
- recorded: <decisions / lessons entries>
```
