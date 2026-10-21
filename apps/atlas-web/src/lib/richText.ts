/**
 * Minimal Markdown subset for assistant answers: headings, paragraphs, bullet and numbered lists,
 * pipe tables, and horizontal rules. Parsing is pure so it can be tested without a DOM; rendering
 * (RichText.tsx) only creates React elements, so model text is never injected as HTML.
 */

export type RichBlock =
  | { kind: 'heading'; text: string }
  | { kind: 'paragraph'; lines: string[] }
  | { kind: 'list'; ordered: boolean; items: string[] }
  | { kind: 'table'; header: string[] | null; rows: string[][] }
  | { kind: 'rule' }

const HEADING = /^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/
const ORDERED = /^\s*\d+[.)]\s+(.*)$/
const BULLET = /^\s*[-•*]\s+(.*)$/
const SEPARATOR_CELL = /^:?-{2,}:?$/

function isTableLine(line: string): boolean {
  return line.trim().startsWith('|')
}

function isSeparatorRow(cells: string[]): boolean {
  return cells.length > 0 && cells.every((cell) => SEPARATOR_CELL.test(cell.replace(/\s/g, '')))
}

/** Cells of one pipe-table row; `\|` stays a literal pipe inside a cell. */
export function splitTableRow(line: string): string[] {
  let body = line.trim()
  if (body.startsWith('|')) body = body.slice(1)
  if (body.endsWith('|') && !body.endsWith('\\|')) body = body.slice(0, -1)
  return body.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'))
}

export function parseRichText(text: string): RichBlock[] {
  const blocks: RichBlock[] = []
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  let paragraph: string[] = []
  let list: { ordered: boolean; items: string[] } | null = null

  const flushParagraph = () => {
    if (paragraph.length) blocks.push({ kind: 'paragraph', lines: paragraph })
    paragraph = []
  }
  const flushList = () => {
    if (list) blocks.push({ kind: 'list', ordered: list.ordered, items: list.items })
    list = null
  }
  const addListItem = (ordered: boolean, item: string) => {
    flushParagraph()
    if (list && list.ordered !== ordered) flushList()
    if (!list) list = { ordered, items: [] }
    list.items.push(item)
  }

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]
    if (isTableLine(line)) {
      flushParagraph()
      flushList()
      const rows: string[][] = []
      while (i < lines.length && isTableLine(lines[i])) {
        rows.push(splitTableRow(lines[i]))
        i += 1
      }
      i -= 1
      const hasHeader = rows.length > 1 && isSeparatorRow(rows[1])
      const body = (hasHeader ? rows.slice(2) : rows).filter((row) => !isSeparatorRow(row))
      blocks.push({ kind: 'table', header: hasHeader ? rows[0] : null, rows: body })
      continue
    }
    if (line.trim() === '') {
      flushParagraph()
      flushList()
      continue
    }
    const heading = HEADING.exec(line)
    if (heading) {
      flushParagraph()
      flushList()
      blocks.push({ kind: 'heading', text: heading[1] })
      continue
    }
    if (RULE.test(line)) {
      flushParagraph()
      flushList()
      blocks.push({ kind: 'rule' })
      continue
    }
    const ordered = ORDERED.exec(line)
    if (ordered) {
      addListItem(true, ordered[1])
      continue
    }
    const bullet = BULLET.exec(line)
    if (bullet) {
      addListItem(false, bullet[1])
      continue
    }
    flushList()
    paragraph.push(line)
  }
  flushParagraph()
  flushList()
  return blocks
}

/** Inline segments: **bold**, `code`, and plain text with Markdown escapes removed. */
export type InlineSegment = { kind: 'text' | 'bold' | 'code'; text: string }

const unescapeMarkdown = (value: string) => value.replace(/\\([\\*_#|`[\]])/g, '$1')

export function parseInline(text: string): InlineSegment[] {
  return text
    .split(/(\*\*(?:\\\*|[^*])+\*\*|`[^`]+`)/g)
    .filter((part) => part !== '')
    .map((part) => {
      if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) {
        return { kind: 'bold', text: unescapeMarkdown(part.slice(2, -2)) }
      }
      if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
        return { kind: 'code', text: part.slice(1, -1) }
      }
      return { kind: 'text', text: unescapeMarkdown(part) }
    })
}
