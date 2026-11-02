




import { describe, expect, it } from 'vitest'

import { findStableBoundary } from '../components/streamingMarkdown.js'











import { DEFAULT_THEME } from '../theme.js'

describe('findStableBoundary', () => {
  it('returns -1 when no blank line exists yet', () => {
    expect(findStableBoundary('partial line with no newline yet')).toBe(-1)
  })

  it('returns -1 when only single newlines exist', () => {
    expect(findStableBoundary('line one\nline two\nline three')).toBe(-1)
  })

  it('splits after the last blank line separator', () => {

    const text = 'first paragraph\n\nsecond paragraph\n\nthird'
    const idx = findStableBoundary(text)

    expect(text.slice(0, idx)).toBe('first paragraph\n\nsecond paragraph\n\n')
    expect(text.slice(idx)).toBe('third')
  })

  it('refuses to split inside an open fenced block', () => {

    const text = '```ts\nfn();\n\nmore code here'

    expect(findStableBoundary(text)).toBe(-1)
  })

  it('splits before an open fenced block but not inside', () => {
    const text = 'intro paragraph\n\n```ts\nfn();\n\nmore code'
    const idx = findStableBoundary(text)

    expect(text.slice(0, idx)).toBe('intro paragraph\n\n')
    expect(text.slice(idx).startsWith('```ts')).toBe(true)
  })

  it('allows splitting after a fenced block closes', () => {
    const text = '```ts\nfn();\n```\n\nnarration continues'
    const idx = findStableBoundary(text)

    expect(text.slice(0, idx)).toBe('```ts\nfn();\n```\n\n')
    expect(text.slice(idx)).toBe('narration continues')
  })

  it('walks backwards through nested fence boundaries safely', () => {


    const text = '```js\na\n```\n\nmid text\n\n```python\nstill open'
    const idx = findStableBoundary(text)

    expect(text.slice(0, idx)).toBe('```js\na\n```\n\nmid text\n\n')
  })

  it('handles empty input', () => {
    expect(findStableBoundary('')).toBe(-1)
  })

  it('refuses to split inside an open $$ math block', () => {


    const text = '$$\nx + y\n\nmore math'

    expect(findStableBoundary(text)).toBe(-1)
  })

  it('allows splitting after a $$ math block closes', () => {
    const text = '$$\nx + y = z\n$$\n\nnarration continues'
    const idx = findStableBoundary(text)

    expect(text.slice(0, idx)).toBe('$$\nx + y = z\n$$\n\n')
    expect(text.slice(idx)).toBe('narration continues')
  })

  it('splits before an open $$ block but not inside', () => {


    const text = 'intro paragraph\n\n$$\nx + y\n\nmore'
    const idx = findStableBoundary(text)

    expect(text.slice(0, idx)).toBe('intro paragraph\n\n')
    expect(text.slice(idx).startsWith('$$')).toBe(true)
  })

  it('treats single-line $$x$$ as zero net toggle', () => {


    const text = 'intro\n\n$$x = y$$\n\nnarration'
    const idx = findStableBoundary(text)

    expect(text.slice(0, idx)).toBe('intro\n\n$$x = y$$\n\n')
    expect(text.slice(idx)).toBe('narration')
  })

  it('refuses to split inside an open \\[ math block', () => {
    const text = '\\[\nx + y\n\nmore'

    expect(findStableBoundary(text)).toBe(-1)
  })
})

describe('streaming theme assumption', () => {
  it('theme is exportable (component import sanity check)', () => {



    expect(DEFAULT_THEME.color.accent).toBeTruthy()
  })
})
