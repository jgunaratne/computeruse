/**
 * Minimal Markdown → React renderer for chat replies.
 *
 * Covers what models actually emit: paragraphs, headings, bullet / numbered lists (one level of
 * nesting), fenced code, block quotes, pipe tables, horizontal rules, and inline code / bold /
 * italic / strikethrough / links. Everything is rendered as React text nodes — no raw HTML ever
 * reaches the DOM — and links are limited to http(s) URLs.
 */

import { createElement, Fragment, type ReactNode } from "react";

const FENCE = /^\s*(```|~~~)\s*([\w+.-]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.+?)\s*#*\s*$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const LIST_ITEM = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
const QUOTE = /^\s*>\s?(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

export function Markdown({ text, className }: { text: string; className?: string }) {
  return <div className={className ? `md ${className}` : "md"}>{renderBlocks(text)}</div>;
}

function startsBlock(line: string): boolean {
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || LIST_ITEM.test(line) || QUOTE.test(line);
}

export function renderBlocks(src: string): ReactNode[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) buf.push(lines[i++]);
      i++; // closing fence (or EOF)
      out.push(
        <pre key={key++} className="md-code" data-lang={fence[2] || undefined}>
          <code>{buf.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6); // # → h3: chat text sits under the page's own headings
      out.push(createElement(`h${level}`, { key: key++, className: "md-h" }, renderInline(heading[2])));
      i++;
      continue;
    }
    if (RULE.test(line)) {
      out.push(<hr key={key++} />);
      i++;
      continue;
    }
    if (QUOTE.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i])) buf.push(QUOTE.exec(lines[i++])![1]);
      out.push(<blockquote key={key++}>{renderBlocks(buf.join("\n"))}</blockquote>);
      continue;
    }
    if (LIST_ITEM.test(line)) {
      const items: { indent: number; ordered: boolean; text: string }[] = [];
      while (i < lines.length) {
        const m = LIST_ITEM.exec(lines[i]);
        if (m) {
          items.push({ indent: m[1].replace(/\t/g, "  ").length, ordered: /\d/.test(m[2]), text: m[3] });
          i++;
        } else if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) {
          items[items.length - 1].text += ` ${lines[i].trim()}`; // lazy continuation line
          i++;
        } else break;
      }
      out.push(<Fragment key={key++}>{renderList(items)}</Fragment>);
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const header = splitRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
      out.push(
        <table key={key++} className="md-table">
          <thead>
            <tr>
              {header.map((c, j) => (
                <th key={j}>{renderInline(c)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>
                {header.map((_, j) => (
                  <td key={j}>{renderInline(r[j] ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }
    const buf: string[] = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) buf.push(lines[i++]);
    out.push(<p key={key++}>{renderInline(buf.join("\n"))}</p>);
  }
  return out;
}

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

function renderList(items: { indent: number; ordered: boolean; text: string }[]): ReactNode {
  if (!items.length) return null;
  const base = items[0].indent;
  const Tag = items[0].ordered ? "ol" : "ul";
  const nodes: ReactNode[] = [];
  let k = 0;
  while (k < items.length) {
    const item = items[k++];
    const nested: typeof items = [];
    while (k < items.length && items[k].indent > base) nested.push(items[k++]);
    nodes.push(
      <li key={nodes.length}>
        {renderInline(item.text)}
        {nested.length > 0 && renderList(nested)}
      </li>,
    );
  }
  return <Tag>{nodes}</Tag>;
}

// --- inline ---------------------------------------------------------------------------------------

const INLINE =
  /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([^*\n]+?)\*\*|__([^_\n]+?)__|(?<![\w*])\*([^*\n]+?)\*(?![\w*])|(?<![\w_])_([^_\n]+?)_(?![\w_])|~~([^~\n]+?)~~|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

export function renderInline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  const pushText = (s: string) => {
    if (!s) return;
    const parts = s.split("\n");
    parts.forEach((p, idx) => {
      if (idx > 0) out.push(<br key={key++} />);
      if (p) out.push(p);
    });
  };
  for (const m of text.matchAll(INLINE)) {
    pushText(text.slice(last, m.index));
    last = m.index! + m[0].length;
    if (m[2] !== undefined) out.push(<code key={key++}>{m[2]}</code>);
    else if (m[3] !== undefined || m[4] !== undefined) out.push(<strong key={key++}>{renderInline(m[3] ?? m[4])}</strong>);
    else if (m[5] !== undefined || m[6] !== undefined) out.push(<em key={key++}>{renderInline(m[5] ?? m[6])}</em>);
    else if (m[7] !== undefined) out.push(<del key={key++}>{renderInline(m[7])}</del>);
    else if (m[8] !== undefined)
      out.push(
        <a key={key++} href={m[9]} target="_blank" rel="noreferrer noopener">
          {renderInline(m[8])}
        </a>,
      );
    else if (m[10] !== undefined)
      out.push(
        <a key={key++} href={m[10]} target="_blank" rel="noreferrer noopener">
          {m[10]}
        </a>,
      );
  }
  pushText(text.slice(last));
  return out;
}
