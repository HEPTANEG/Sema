





import { describe, expect, it } from 'vitest'

import { env, supportsOsc52Clipboard } from '../../utils/env.js'

import { shouldEmitClipboardSequence, shouldUseNativeClipboard } from './osc.js'

describe('shouldEmitClipboardSequence', () => {
  it('suppresses local multiplexer clipboard OSC by default', () => {
    expect(shouldEmitClipboardSequence({ TMUX: '/tmp/tmux-1/default,1,0' } as NodeJS.ProcessEnv)).toBe(false)
    expect(shouldEmitClipboardSequence({ STY: '1234.pts-0.host' } as NodeJS.ProcessEnv)).toBe(false)
  })

  it('keeps OSC enabled for remote or plain local terminals', () => {
    expect(
      shouldEmitClipboardSequence({ SSH_CONNECTION: '1', TMUX: '/tmp/tmux-1/default,1,0' } as NodeJS.ProcessEnv)
    ).toBe(true)
    expect(shouldEmitClipboardSequence({ TERM: 'xterm-256color' } as NodeJS.ProcessEnv)).toBe(true)
  })

  it('honors explicit env override', () => {
    expect(
      shouldEmitClipboardSequence({
        HERMES_TUI_CLIPBOARD_OSC52: '1',
        TMUX: '/tmp/tmux-1/default,1,0'
      } as NodeJS.ProcessEnv)
    ).toBe(true)
    expect(
      shouldEmitClipboardSequence({ HERMES_TUI_COPY_OSC52: '0', TERM: 'xterm-256color' } as NodeJS.ProcessEnv)
    ).toBe(false)
  })

  it('HERMES_TUI_FORCE_OSC52 takes precedence over TMUX suppression', () => {



    expect(shouldEmitClipboardSequence({ TMUX: '/tmp/t,1,0' } as NodeJS.ProcessEnv)).toBe(false)
    expect(
      shouldEmitClipboardSequence({
        HERMES_TUI_FORCE_OSC52: '1',
        TMUX: '/tmp/t,1,0'
      } as NodeJS.ProcessEnv)
    ).toBe(true)
  })

  it('HERMES_TUI_FORCE_OSC52=0 suppresses OSC 52 even for remote or plain terminals', () => {
    expect(
      shouldEmitClipboardSequence({
        HERMES_TUI_FORCE_OSC52: '0',
        SSH_CONNECTION: '1'
      } as NodeJS.ProcessEnv)
    ).toBe(false)
  })
})

describe('supportsOsc52Clipboard', () => {





  it.each(['ghostty', 'kitty', 'WezTerm', 'windows-terminal', 'vscode'])(
    'returns true for allowlisted terminal %s',
    terminal => {
      expect(supportsOsc52Clipboard(terminal)).toBe(true)
    }
  )




  it.each(['iTerm.app', 'alacritty', 'Apple_Terminal', 'xterm', 'tmux', 'screen', 'cursor', 'WarpTerminal', ''])(
    'returns false for non-allowlisted terminal %s',
    terminal => {
      expect(supportsOsc52Clipboard(terminal)).toBe(false)
    }
  )

  it('returns false when terminal is null (detection failed)', () => {
    expect(supportsOsc52Clipboard(null)).toBe(false)
  })

  it('defaults to the module-level detected terminal when no argument is passed', () => {



    expect(typeof supportsOsc52Clipboard()).toBe('boolean')
    expect(supportsOsc52Clipboard()).toBe(supportsOsc52Clipboard(env.terminal))
  })
})






describe('shouldUseNativeClipboard', () => {
  it('returns false over SSH (native would write to remote clipboard)', () => {



    expect(shouldUseNativeClipboard({ SSH_CONNECTION: '1' } as NodeJS.ProcessEnv, 'xterm')).toBe(false)
    expect(shouldUseNativeClipboard({ SSH_CONNECTION: '1' } as NodeJS.ProcessEnv, 'ghostty')).toBe(false)
    expect(shouldUseNativeClipboard({ SSH_CONNECTION: '1' } as NodeJS.ProcessEnv, null)).toBe(false)
  })

  it('returns true on plain local terminals (existing behaviour)', () => {




    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, 'xterm')).toBe(true)
    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, 'iTerm.app')).toBe(true)
    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, 'Apple_Terminal')).toBe(true)
    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, 'alacritty')).toBe(true)
    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, null)).toBe(true)
  })

  it('returns false on allowlisted local terminals (the race-fix case)', () => {



    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, 'ghostty')).toBe(false)
    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, 'kitty')).toBe(false)
    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, 'WezTerm')).toBe(false)
    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, 'windows-terminal')).toBe(false)
    expect(shouldUseNativeClipboard({} as NodeJS.ProcessEnv, 'vscode')).toBe(false)
  })

  it('returns true inside tmux even on allowlisted outer terminal', () => {





    expect(shouldUseNativeClipboard({ TMUX: '/tmp/t,1,0' } as NodeJS.ProcessEnv, 'ghostty')).toBe(true)
    expect(shouldUseNativeClipboard({ TMUX: '/tmp/t,1,0' } as NodeJS.ProcessEnv, 'kitty')).toBe(true)
    expect(shouldUseNativeClipboard({ TMUX: '/tmp/t,1,0' } as NodeJS.ProcessEnv, 'WezTerm')).toBe(true)
    expect(shouldUseNativeClipboard({ TMUX: '/tmp/t,1,0' } as NodeJS.ProcessEnv, 'vscode')).toBe(true)
  })

  it('returns true inside GNU screen even on allowlisted outer terminal', () => {


    expect(shouldUseNativeClipboard({ STY: '1234.pts-0.host' } as NodeJS.ProcessEnv, 'ghostty')).toBe(true)
    expect(shouldUseNativeClipboard({ STY: '1234.pts-0.host' } as NodeJS.ProcessEnv, 'kitty')).toBe(true)
  })

  it('returns true when OSC 52 emission is disabled via HERMES_TUI_FORCE_OSC52=0', () => {



    expect(shouldUseNativeClipboard({ HERMES_TUI_FORCE_OSC52: '0' } as NodeJS.ProcessEnv, 'ghostty')).toBe(true)
    expect(shouldUseNativeClipboard({ HERMES_TUI_FORCE_OSC52: '0' } as NodeJS.ProcessEnv, 'kitty')).toBe(true)
    expect(shouldUseNativeClipboard({ HERMES_TUI_CLIPBOARD_OSC52: '0' } as NodeJS.ProcessEnv, 'WezTerm')).toBe(true)
    expect(shouldUseNativeClipboard({ HERMES_TUI_COPY_OSC52: 'no' } as NodeJS.ProcessEnv, 'vscode')).toBe(true)
  })

  it('returns true under TMUX even with HERMES_TUI_FORCE_OSC52=1 on an allowlisted terminal (tmux load-buffer path)', () => {



    expect(
      shouldUseNativeClipboard({ HERMES_TUI_FORCE_OSC52: '1', TMUX: '/tmp/t,1,0' } as NodeJS.ProcessEnv, 'ghostty')
      // TMUX guard wins — native still fires because we're going through
      // tmux load-buffer, not raw OSC 52 to the terminal.
    ).toBe(true)
  })

  it('SSH_CONNECTION takes precedence over allowlisted terminal', () => {


    expect(shouldUseNativeClipboard({ SSH_CONNECTION: '1' } as NodeJS.ProcessEnv, 'ghostty')).toBe(false)
  })

  it('SSH_CONNECTION takes precedence over TMUX', () => {



    expect(shouldUseNativeClipboard({ SSH_CONNECTION: '1', TMUX: '/tmp/t,1,0' } as NodeJS.ProcessEnv, 'xterm')).toBe(
      false
    )
  })

  it('defaults env to process.env and terminal to the module-detected terminal when no args passed', () => {




    expect(typeof shouldUseNativeClipboard()).toBe('boolean')
  })
})
