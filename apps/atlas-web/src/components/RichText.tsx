import { Fragment, type ReactNode } from 'react'

function inline(text: string, keyPrefix: string): ReactNode[] {
  // Only **bold** is interpreted; everything else is rendered as plain (escaped) text.
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith('**') && part.endsWith('**') && part.length > 4 ? (
      <strong key={`${keyPrefix}-${i}`}>{part.slice(2, -2)}</strong>
    ) : (
      <Fragment key={`${keyPrefix}-${i}`}>{part}</Fragment>
    ),
  )
}

/** Minimal, safe rendering of assistant text: paragraphs, "- " bullet lists and **bold**. */
export function RichText({ text }: { text: string }) {
  const blocks: ReactNode[] = []
  let list: string[] = []
  let paragraph: string[] = []
  const flushList = () => {
    if (!list.length) return
    const items = list
    blocks.push(
      <ul key={`ul-${blocks.length}`}>
        {items.map((item, i) => (
          <li key={i}>{inline(item, `li-${blocks.length}-${i}`)}</li>
        ))}
      </ul>,
    )
    list = []
  }
  const flushParagraph = () => {
    if (!paragraph.length) return
    const lines = paragraph
    blocks.push(
      <p key={`p-${blocks.length}`}>
        {lines.map((line, i) => (
          <Fragment key={i}>
            {i > 0 && <br />}
            {inline(line, `p-${blocks.length}-${i}`)}
          </Fragment>
        ))}
      </p>,
    )
    paragraph = []
  }
  for (const line of text.split('\n')) {
    const bullet = /^\s*[-•]\s+(.*)$/.exec(line)
    if (bullet) {
      flushParagraph()
      list.push(bullet[1])
    } else if (line.trim() === '') {
      flushList()
      flushParagraph()
    } else {
      flushList()
      paragraph.push(line)
    }
  }
  flushList()
  flushParagraph()
  return <>{blocks}</>
}
