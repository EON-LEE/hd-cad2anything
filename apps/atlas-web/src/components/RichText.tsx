import { Fragment, type ReactNode } from 'react'
import { parseInline, parseRichText } from '../lib/richText'

function inline(text: string, keyPrefix: string): ReactNode[] {
  return parseInline(text).map((segment, i) => {
    const key = `${keyPrefix}-${i}`
    if (segment.kind === 'bold') return <strong key={key}>{segment.text}</strong>
    if (segment.kind === 'code') return <code key={key}>{segment.text}</code>
    return <Fragment key={key}>{segment.text}</Fragment>
  })
}

/**
 * Safe rendering of assistant text: headings, paragraphs, lists, pipe tables, rules, **bold**, `code`.
 * `trailing` (e.g. a streaming cursor) is placed at the end of the last block's text.
 */
export function RichText({ text, trailing }: { text: string; trailing?: ReactNode }) {
  const blocks = parseRichText(text)
  return (
    <>
      {blocks.map((block, b) => {
        const key = `b${b}`
        const tail = b === blocks.length - 1 ? trailing : null
        switch (block.kind) {
          case 'heading':
            return (
              <h4 key={key} className="rich-heading">
                {inline(block.text, key)}
                {tail}
              </h4>
            )
          case 'rule':
            return (
              <Fragment key={key}>
                <hr />
                {tail}
              </Fragment>
            )
          case 'list': {
            const items = block.items.map((item, i) => (
              <li key={i}>
                {inline(item, `${key}-${i}`)}
                {i === block.items.length - 1 ? tail : null}
              </li>
            ))
            return block.ordered ? <ol key={key}>{items}</ol> : <ul key={key}>{items}</ul>
          }
          case 'table':
            return (
              <Fragment key={key}>
                <div className="rich-table">
                  <table>
                    {block.header && (
                      <thead>
                        <tr>
                          {block.header.map((cell, i) => (
                            <th key={i} scope="col">
                              {inline(cell, `${key}-h${i}`)}
                            </th>
                          ))}
                        </tr>
                      </thead>
                    )}
                    <tbody>
                      {block.rows.map((row, r) => (
                        <tr key={r}>
                          {row.map((cell, i) => (
                            <td key={i}>{inline(cell, `${key}-${r}-${i}`)}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {tail}
              </Fragment>
            )
          default:
            return (
              <p key={key}>
                {block.lines.map((line, i) => (
                  <Fragment key={i}>
                    {i > 0 && <br />}
                    {inline(line, `${key}-${i}`)}
                  </Fragment>
                ))}
                {tail}
              </p>
            )
        }
      })}
      {blocks.length === 0 && trailing}
    </>
  )
}
