#!/usr/bin/env node
/**
 * craft-guard.mjs — PreToolUse guard for the Craft Design System, for both Claude Code and
 * Gemini/Antigravity. One script, two protocols, detected from the payload:
 *
 *   Claude Code:  stdin {tool_name, tool_input, cwd}; exit 2 + stderr blocks the call.
 *   Antigravity:  stdin {toolCall: {name, args}, workspacePaths}; stdout {"decision":"deny","reason"}.
 *
 * What it keeps agents from doing:
 *   1. Editing the vendored kit (craft-ds.json → kit.dir): tokens, css, js, icons, fonts, licences.
 *      The kit is upgraded with `craft-ds update .`, never hand-edited.
 *   2. Writing into the kit from a shell (redirects, sed -i, tee, cp/mv onto it, rm -r of it).
 *   3. Editing `.craft-ds-baseline.json` by hand (it is written by `craft-ds probe --update-baseline`).
 *
 * Installed by `craft-ds apply` as .claude/hooks/craft-guard.mjs and .agents/hooks/craft-guard.mjs.
 * Node ≥ 18, no dependencies. Keep it fast: it runs before every tool call.
 */
import { existsSync, readFileSync } from 'node:fs'
import { dirname, isAbsolute, join, relative, resolve } from 'node:path'

const raw = await new Response(process.stdin).text().catch(() => '')
let input = {}
try { input = JSON.parse(raw || '{}') } catch { input = {} }

const isAntigravity = Boolean(input.toolCall)
const tool = isAntigravity ? String(input.toolCall.name ?? '') : String(input.tool_name ?? '')
const args = isAntigravity ? (input.toolCall.args ?? {}) : (input.tool_input ?? {})

/** The repo root: where craft-ds.json lives (walk up from the hint). */
function findRoot(hint) {
  let dir = hint ? resolve(hint) : process.cwd()
  for (let i = 0; i < 12; i++) {
    if (existsSync(join(dir, 'craft-ds.json'))) return dir
    const up = dirname(dir)
    if (up === dir) break
    dir = up
  }
  return null
}
const hint = process.env.CLAUDE_PROJECT_DIR || input.cwd || (Array.isArray(input.workspacePaths) && input.workspacePaths[0]) || process.cwd()
const root = findRoot(hint)
if (!root) allow() // not a craft-ds project: nothing to guard

let manifest = {}
try { manifest = JSON.parse(readFileSync(join(root, 'craft-ds.json'), 'utf8')) } catch { allow() }
const kitDir = ((manifest.kit && manifest.kit.dir) || 'vendor/craft-ds').replace(/\/+$/, '')
const isSelf = kitDir === '.' || kitDir === ''
// When the repo *is* the kit, protect the kit files by name; otherwise the whole kit folder.
const KIT_FILES = ['css/craft-ds.css', 'js/craft-ds.js', 'js/tokens-data.js', 'js/icons.js', 'tokens/tokens.json', 'LICENSE-APACHE', 'LICENSE-MIT']
const KIT_DIRS = ['assets/fonts', 'assets/icons', 'assets/brand']

function toRel(p) {
  if (!p) return null
  const abs = isAbsolute(p) ? p : resolve(root, p)
  const r = relative(root, abs).split('\\').join('/')
  return r.startsWith('..') ? null : r
}
function inKit(rel) {
  if (rel == null) return false
  if (isSelf) return KIT_FILES.includes(rel) || KIT_DIRS.some((d) => rel.startsWith(d + '/'))
  return rel === kitDir || rel.startsWith(kitDir + '/')
}
const WHY_KIT = isSelf
  ? 'that file is part of the Craft DS kit: tokens.json, tokens-data.js, craft-ds.css and icons.js are generated together from the PhotoCraft source (see CONVENTIONS.md). Change the generator or ask the user.'
  : `\`${kitDir}/\` is the vendored Craft Design System and is read-only. Compose with its tokens and classes in your own files; upgrade it with \`craft-ds update .\`.`

// 1. file tools
const FILE_TOOLS = /^(Edit|Write|MultiEdit|NotebookEdit|write_file|edit_file|replace_file_content|multi_replace_file_content|write_to_file|notebook_edit|apply_patch)$/i
if (FILE_TOOLS.test(tool)) {
  const target = args.file_path ?? args.notebook_path ?? args.Path ?? args.TargetFile ?? args.NotebookPath ?? args.AbsolutePath ?? args.path
  const rel = toRel(target)
  if (inKit(rel)) deny(`${rel}: ${WHY_KIT}`)
  if (rel === '.craft-ds-baseline.json') deny('the drift baseline is written by `craft-ds probe . --update-baseline`, not by hand: fix the drift or run that command and say so.')
}

// 2. shell tools: judge commands, not text
const SHELL_TOOLS = /^(Bash|run_command|run_terminal_cmd|execute_command|shell)$/i
if (SHELL_TOOLS.test(tool)) {
  const text = String(args.command ?? args.CommandLine ?? args.cmd ?? '')
  const bare = text.replace(/<<-?\s*(['"]?)(\w+)\1[^\n]*\n[\s\S]*?\n\2(?=\n|$)/g, '')
  const commands = bare
    .replace(/'[^']*'|"(?:[^"\\]|\\.)*"/g, '""')
    .split(/\n|;|&&|\|\||\$\(|`/)
    .map((c) => c.trim())
    .filter(Boolean)
  const kitRe = isSelf
    ? new RegExp(`(^|[\\s=/])(?:\\./)?(${[...KIT_FILES, ...KIT_DIRS.map((d) => d + '/')].map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`)
    : new RegExp(`(^|[\\s=/])(?:\\./)?${kitDir.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(/|\\b)`)
  for (const c of commands) {
    if (!kitRe.test(c)) continue
    const writes =
      /(^|\s)(>>?|\|)\s*\S*/.test(c) && /(>>?)\s*\S*(?:vendor|css|js|tokens|assets|LICENSE)/.test(c) ||
      /^(sed\s+(-[a-zA-Z]*i|--in-place)|tee\b|cp\b|mv\b|rm\b|rmdir\b|truncate\b|chmod\b|ln\b|install\b|rsync\b|dd\b|python3?\s+-c|perl\s+-[a-z]*i)/.test(c)
    if (writes) deny(`\`${c.slice(0, 80)}\`: ${WHY_KIT}`)
  }
}

allow()

function allow() {
  if (isAntigravity) process.stdout.write(JSON.stringify({ decision: 'allow' }))
  process.exit(0)
}
function deny(why) {
  const reason = `Blocked by craft-guard.mjs: ${why}`
  if (isAntigravity) {
    process.stdout.write(JSON.stringify({ decision: 'deny', reason }))
    process.exit(0)
  }
  process.stderr.write(reason + '\n')
  process.exit(2)
}
