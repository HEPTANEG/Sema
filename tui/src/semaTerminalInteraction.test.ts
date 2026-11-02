import { afterEach, describe, expect, it, vi } from 'vitest'

import { applySemaSlashResult, decideSemaCtrlC, resolveSemaSlashInput, semaSlashCompletions } from './SemaRavenApp.js'
import { isPlainTextInsertable } from './raven/components/textInput.js'
import { $activeView, $commandOutput, $panelScroll, $transcript } from './stores/runtime_state.js'

describe('Sema terminal interaction defaults', () => {
  afterEach(() => {
    $activeView.set('chat')
    $commandOutput.set(null)
    $panelScroll.set(0)
    $transcript.set([])
    delete process.env.RAVEN_TUI_DISABLE_MOUSE
    delete process.env.RAVEN_TUI_INLINE
    delete process.env.SEMA_TUI_INLINE
  })

  it('uses alternate-screen rendering by default to avoid stale frame residue', async () => {
    vi.resetModules()
    delete process.env.SEMA_TUI_INLINE
    delete process.env.RAVEN_TUI_INLINE

    const env = await import('./raven/config/env.js')

    expect(env.INLINE_MODE).toBe(false)
  })

  it('allows opting into inline diagnostics explicitly', async () => {
    vi.resetModules()
    process.env.SEMA_TUI_INLINE = '1'
    delete process.env.RAVEN_TUI_INLINE

    const env = await import('./raven/config/env.js')

    expect(env.INLINE_MODE).toBe(true)
    delete process.env.SEMA_TUI_INLINE
  })

  it('keeps the Raven inline override working', async () => {
    vi.resetModules()
    delete process.env.SEMA_TUI_INLINE
    process.env.RAVEN_TUI_INLINE = '1'

    const env = await import('./raven/config/env.js')

    expect(env.INLINE_MODE).toBe(true)
    delete process.env.RAVEN_TUI_INLINE
  })

  it('keeps mouse tracking on by default so the TUI scrollbar can receive wheel events', async () => {
    vi.resetModules()
    delete process.env.RAVEN_TUI_DISABLE_MOUSE

    const env = await import('./raven/config/env.js')

    expect(env.MOUSE_TRACKING).toBe(true)
  })

  it('allows mouse tracking opt-out', async () => {
    vi.resetModules()
    process.env.RAVEN_TUI_DISABLE_MOUSE = '1'

    const env = await import('./raven/config/env.js')

    expect(env.MOUSE_TRACKING).toBe(false)
    delete process.env.RAVEN_TUI_DISABLE_MOUSE
  })

  it('does not treat Ctrl+C as clear, cancel, or exit when nothing is selected', () => {
    expect(decideSemaCtrlC({ hasInputSelection: false, hasTerminalSelection: false })).toBe('noop')
  })

  it('copies visible selections before falling back to no-op', () => {
    expect(decideSemaCtrlC({ hasInputSelection: false, hasTerminalSelection: true })).toBe('copy-terminal-selection')
    expect(decideSemaCtrlC({ hasInputSelection: true, hasTerminalSelection: false })).toBe('copy-input-selection')
  })

  it('shows slash command content in the temporary command output panel, not the transcript', () => {
    const result = applySemaSlashResult('/memory focus status', {
      content: 'debug focus status',
      title: 'Focus status'
    })

    expect(result.shouldExit).toBe(false)
    expect($activeView.get()).toBe('commandOutput')
    expect($commandOutput.get()).toEqual({title: 'Focus status', content: 'debug focus status'})
    expect($transcript.get()).toEqual([])
  })

  it('matches slash completions against the full command text and keeps the best match at the bottom', () => {
    const commands = [
      {name: '/memory status', help: 'status'},
      {name: '/memory context status', help: 'context'},
      {name: '/memory ltm maintain', help: 'maintain'},
      {name: '/memory ltm visualize', help: 'visualize'}
    ]

    const completions = semaSlashCompletions(commands, '/memory ltm')

    expect(completions.map(item => item.display)).toEqual([
      '/memory ltm visualize',
      '/memory ltm maintain'
    ])
    expect(completions.at(-1)?.display).toBe('/memory ltm maintain')
  })

  it('resolves incomplete and typo slash input before sending it to the backend', () => {
    const commands = [
      {name: '/memory ltm maintain', help: 'maintain'},
      {name: '/memory ltm visualize', help: 'visualize'},
      {name: '/status', help: 'status'}
    ]

    expect(resolveSemaSlashInput(commands, '/memory ltm visual')).toBe('/memory ltm visualize')
    expect(resolveSemaSlashInput(commands, '/stauts')).toBe('/status')
  })

  it('submits the bottom-most visible slash completion for incomplete grouped commands', () => {
    const commands = [
      {name: '/memory', help: 'memory'},
      {name: '/memory status', help: 'status'},
      {name: '/memory ltm maintain', help: 'maintain'},
      {name: '/memory ltm visualize', help: 'visualize'}
    ]

    expect(semaSlashCompletions(commands, '/memory ltm').at(-1)?.display).toBe('/memory ltm maintain')
    expect(resolveSemaSlashInput(commands, '/memory ltm')).toBe('/memory ltm maintain')
  })

  it('allows multiline plain text to use the TextInput paste fallback', () => {
    expect(isPlainTextInsertable('hello 你好')).toBe(true)
    expect(isPlainTextInsertable('line one\nline two\n')).toBe(true)
    expect(isPlainTextInsertable('')).toBe(false)
    expect(isPlainTextInsertable('ok\x1b[31m')).toBe(false)
  })
})
