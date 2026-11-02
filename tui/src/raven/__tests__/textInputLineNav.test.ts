




import { describe, expect, it } from 'vitest'

import { lineNav } from '../components/textInput.js'

describe('lineNav', () => {
  it('returns null for single-line input (up)', () => {
    expect(lineNav('hello world', 6, -1)).toBeNull()
  })

  it('returns null for single-line input (down)', () => {
    expect(lineNav('hello world', 6, 1)).toBeNull()
  })

  it('returns null when cursor already on first line of a multiline block', () => {
    expect(lineNav('one\ntwo\nthree', 2, -1)).toBeNull()
  })

  it('returns null when cursor on last line of a multiline block', () => {
    expect(lineNav('one\ntwo\nthree', 10, 1)).toBeNull()
  })

  it('moves cursor up one line preserving column', () => {

    expect(lineNav('hello\nworld', 9, -1)).toBe(3)
  })

  it('moves cursor down one line preserving column', () => {

    expect(lineNav('hello\nworld', 2, 1)).toBe(8)
  })

  it('clamps to end of shorter destination line on up', () => {

    const s = 'abc\nlong long text'
    const from = 14

    expect(lineNav(s, from, -1)).toBe(3)
  })

  it('clamps to end of shorter destination line on down', () => {

    const s = 'long long text\nabc'

    expect(lineNav(s, 10, 1)).toBe(18)
  })

  it('handles empty lines correctly', () => {

    expect(lineNav('a\n\nb', 3, -1)).toBe(2)
  })

  it('handles leading newline without crashing', () => {
    expect(lineNav('\nfoo', 2, -1)).toBe(0)
  })
})
