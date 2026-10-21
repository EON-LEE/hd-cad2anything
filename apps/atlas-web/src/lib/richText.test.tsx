import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { RichText } from '../components/RichText'
import { parseInline, parseRichText, splitTableRow } from './richText'

describe('parseRichText', () => {
  it('splits headings, lists, rules, and paragraphs', () => {
    const blocks = parseRichText('**결론**\n### 화면에서 볼 것\n- 하나\n- 둘\n1. 첫째\n2. 둘째\n---\n문단 첫 줄\n둘째 줄')
    expect(blocks.map((b) => b.kind)).toEqual(['paragraph', 'heading', 'list', 'list', 'rule', 'paragraph'])
    expect(blocks[1]).toEqual({ kind: 'heading', text: '화면에서 볼 것' })
    expect(blocks[2]).toEqual({ kind: 'list', ordered: false, items: ['하나', '둘'] })
    expect(blocks[3]).toEqual({ kind: 'list', ordered: true, items: ['첫째', '둘째'] })
    expect(blocks[5]).toEqual({ kind: 'paragraph', lines: ['문단 첫 줄', '둘째 줄'] })
  })

  it('parses pipe tables with a header separator and escaped pipes', () => {
    const [table] = parseRichText('| 매장 | ㎡당 매출 |\n|---|---:|\n| 4F-E01 | 1,000 |\n| a \\| b | 2 |')
    expect(table).toEqual({ kind: 'table', header: ['매장', '㎡당 매출'], rows: [['4F-E01', '1,000'], ['a | b', '2']] })
    expect(splitTableRow('| x | y')).toEqual(['x', 'y'])
  })

  it('keeps tables without a header row as body rows', () => {
    const [table] = parseRichText('| 1F | 10 |\n| 2F | 20 |')
    expect(table).toEqual({ kind: 'table', header: null, rows: [['1F', '10'], ['2F', '20']] })
  })

  it('parses bold, code, and Markdown escapes inline', () => {
    expect(parseInline('**+2.8%**로 `analytics_drivers` 결과 \\* 주석')).toEqual([
      { kind: 'bold', text: '+2.8%' },
      { kind: 'text', text: '로 ' },
      { kind: 'code', text: 'analytics_drivers' },
      { kind: 'text', text: ' 결과 * 주석' },
    ])
  })
})

describe('RichText', () => {
  it('renders tables and headings and escapes HTML in model text', () => {
    const html = renderToStaticMarkup(<RichText text={'### 요약\n| 항목 | 값 |\n|---|---|\n| <b>x</b> | **1** |'} />)
    expect(html).toContain('<h4 class="rich-heading">요약</h4>')
    expect(html).toContain('<th scope="col">항목</th>')
    expect(html).toContain('&lt;b&gt;x&lt;/b&gt;')
    expect(html).toContain('<strong>1</strong>')
    expect(html).not.toContain('<b>')
  })

  it('puts a trailing node (the streaming cursor) at the end of the last block', () => {
    const cursor = <i className="cursor" />
    expect(renderToStaticMarkup(<RichText text={'첫 문단\n\n둘째 문단'} trailing={cursor} />)).toBe('<p>첫 문단</p><p>둘째 문단<i class="cursor"></i></p>')
    expect(renderToStaticMarkup(<RichText text={'- 하나\n- 둘'} trailing={cursor} />)).toBe('<ul><li>하나</li><li>둘<i class="cursor"></i></li></ul>')
    expect(renderToStaticMarkup(<RichText text="" trailing={cursor} />)).toBe('<i class="cursor"></i>')
  })
})
