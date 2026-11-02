











import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import type { append, load } from '../lib/history.js'

interface HistoryModule {
  append: typeof append
  load: typeof load
}

let history: HistoryModule

beforeAll(async () => {
  const dir = mkdtempSync(join(tmpdir(), 'raven-hist-'))

  vi.stubEnv('RAVEN_HOME', dir)
  history = await import('../lib/history.js')
})

afterAll(() => {
  vi.unstubAllEnvs()
})

describe('input history store (backs cycleHistory)', () => {
  it('starts empty when no history file exists', () => {
    expect(history.load()).toEqual([])
  })

  it('appends entries in insertion order with newest last', () => {
    history.append('first command')
    history.append('second command')
    history.append('third command')

    const entries = history.load()

    expect(entries).toEqual(['first command', 'second command', 'third command'])

    expect(entries.at(-1)).toBe('third command')

    expect(entries[0]).toBe('first command')
  })

  it('deduplicates a repeat of the most recent entry', () => {
    history.append('third command')

    expect(history.load()).toEqual(['first command', 'second command', 'third command'])
  })

  it('trims leading/trailing whitespace and ignores blank input', () => {
    history.append('   ')
    history.append('  spaced command  ')

    const entries = history.load()

    expect(entries.at(-1)).toBe('spaced command')
    expect(entries).not.toContain('   ')
  })

  it('round-trips a multi-line entry through the on-disk format', async () => {
    history.append('line a\nline b')

    vi.resetModules()
    const reloaded: HistoryModule = await import('../lib/history.js')

    expect(reloaded.load().at(-1)).toBe('line a\nline b')
    expect(reloaded.load()).toEqual([
      'first command',
      'second command',
      'third command',
      'spaced command',
      'line a\nline b'
    ])
  })
})
