



import { render } from 'ink-testing-library'
import React from 'react'
import { describe, expect, it } from 'vitest'

import type { Episode } from '../types.js'

import { EpisodeView } from '../components/episodeView.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const frame = (node: React.ReactElement) => stripAnsi(render(node).lastFrame() ?? '')

const noNarrationStep: Episode = {
  index: 0,
  reasoning: 'SECRET internal plan',
  narration: '',
  reasoningMs: 8000,
  tools: [
    { id: 'a', name: 'read_file', summary: 'a.go', ok: true, done: true, resultPreview: 'package a' },
    { id: 'b', name: 'read_file', summary: 'b.go', ok: true, done: true }
  ]
}

const narratedStep: Episode = {
  index: 1,
  reasoning: 'THE chain of thought',
  narration: 'I will read the controller files',
  reasoningMs: 3000,
  tools: [{ id: 'c', name: 'read_file', summary: 'ctrl.go', ok: true, done: true, resultPreview: 'package ctrl' }]
}

describe('EpisodeView (flat, one-level)', () => {
  it('collapses a no-narration step to one summary line', () => {
    const f = frame(<EpisodeView episodes={[noNarrationStep]} t={DEFAULT_THEME} text="FINAL ANSWER" />)

    expect(f).toContain('reasoning for 8s')
    expect(f).toContain('read 2 files')

    expect(f).toContain('FINAL ANSWER')

    expect(f).not.toContain('SECRET internal plan')
    expect(f).not.toContain('package a')
  })

  it('expands a narrated step: reasoning fold before narration, narration prose, tools with inline result', () => {
    const f = frame(<EpisodeView episodes={[narratedStep]} t={DEFAULT_THEME} />)
    expect(f).toContain('reasoning for 3s')
    expect(f).toContain('I will read the controller files')
    expect(f).toContain('read')
    expect(f).toContain('ctrl.go')
    expect(f).toContain('package ctrl')

    expect(f).not.toContain('THE chain of thought')

    expect(f.indexOf('reasoning for 3s')).toBeLessThan(f.indexOf('I will read the controller files'))
  })

  it('renders only the final answer when there are no steps', () => {
    const f = frame(<EpisodeView episodes={[]} t={DEFAULT_THEME} text="just an answer" />)
    expect(f).toContain('just an answer')
    expect(f).not.toContain('reasoning')
  })

  it('live: the running (last) step is expanded and streams its text', () => {
    const f = frame(<EpisodeView episodes={[noNarrationStep]} live t={DEFAULT_THEME} text="LIVE OUTPUT" />)

    expect(f).toContain('a.go')

    expect(f).toContain('LIVE OUTPUT')
  })
})
