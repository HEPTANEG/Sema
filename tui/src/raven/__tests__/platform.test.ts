




import { afterEach, describe, expect, it, vi } from 'vitest'

const originalPlatform = process.platform

async function importPlatform(platform: NodeJS.Platform) {
  vi.resetModules()
  Object.defineProperty(process, 'platform', { value: platform })

  return import('../lib/platform.js')
}

afterEach(() => {
  Object.defineProperty(process, 'platform', { value: originalPlatform })
  vi.resetModules()
})

describe('platform action modifier', () => {
  it('treats kitty Cmd sequences as the macOS action modifier', async () => {
    const { isActionMod } = await importPlatform('darwin')

    expect(isActionMod({ ctrl: false, meta: false, super: true })).toBe(true)
    expect(isActionMod({ ctrl: false, meta: true, super: false })).toBe(true)
    expect(isActionMod({ ctrl: true, meta: false, super: false })).toBe(false)
  })

  it('still uses Ctrl as the action modifier on non-macOS', async () => {
    const { isActionMod } = await importPlatform('linux')

    expect(isActionMod({ ctrl: true, meta: false, super: false })).toBe(true)
    expect(isActionMod({ ctrl: false, meta: false, super: true })).toBe(false)
  })
})

describe('isCopyShortcut', () => {
  it('keeps Ctrl+C as the local non-macOS copy chord', async () => {
    const { isCopyShortcut } = await importPlatform('linux')

    expect(isCopyShortcut({ ctrl: true, meta: false, super: false }, 'c', {})).toBe(true)
  })

  it('accepts client Cmd+C over SSH even when running on Linux', async () => {
    const { isCopyShortcut } = await importPlatform('linux')
    const env = { SSH_CONNECTION: '1 2 3 4' } as NodeJS.ProcessEnv

    expect(isCopyShortcut({ ctrl: false, meta: false, super: true }, 'c', env)).toBe(true)
    expect(isCopyShortcut({ ctrl: false, meta: true, super: false }, 'c', env)).toBe(true)
  })

  it('does not treat local Linux Alt+C as copy', async () => {
    const { isCopyShortcut } = await importPlatform('linux')

    expect(isCopyShortcut({ ctrl: false, meta: true, super: false }, 'c', {})).toBe(false)
  })

  it('accepts the VS Code/Cursor forwarded Cmd+C copy sequence on macOS', async () => {
    const { isCopyShortcut } = await importPlatform('darwin')

    expect(isCopyShortcut({ ctrl: true, meta: false, super: true }, 'c', {})).toBe(true)
  })
})

describe('isVoiceToggleKey', () => {
  it('matches raw Ctrl+B on macOS (doc-default across platforms)', async () => {
    const { isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b')).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'B')).toBe(true)
  })

  it('matches kitty-style Cmd+B on macOS via key.super', async () => {
    const { isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b')).toBe(true)



    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b')).toBe(false)
  })

  it('matches Ctrl+B on non-macOS platforms', async () => {
    const { isVoiceToggleKey } = await importPlatform('linux')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b')).toBe(true)
  })

  it('does not match unmodified b or other Ctrl combos', async () => {
    const { isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: false }, 'b')).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'a')).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'c')).toBe(false)
  })
})

describe('parseVoiceRecordKey', () => {
  it('falls back to Ctrl+B for empty input', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    expect(parseVoiceRecordKey('')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('parses ctrl+<letter> bindings', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    expect(parseVoiceRecordKey('ctrl+o')).toEqual({ ch: 'o', mod: 'ctrl', raw: 'ctrl+o' })
    expect(parseVoiceRecordKey('Ctrl+R')).toEqual({ ch: 'r', mod: 'ctrl', raw: 'ctrl+r' })
  })

  it('parses alt/super aliases', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    expect(parseVoiceRecordKey('alt+b').mod).toBe('alt')
    expect(parseVoiceRecordKey('option+b').mod).toBe('alt')
    expect(parseVoiceRecordKey('super+b').mod).toBe('super')
    expect(parseVoiceRecordKey('win+b').mod).toBe('super')
  })

  it('treats ambiguous mac modifiers (meta / cmd / command) as unrecognised', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')







    expect(parseVoiceRecordKey('meta+b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('cmd+b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('command+b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('parses named keys (space, enter, tab, escape, backspace, delete)', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')



    expect(parseVoiceRecordKey('ctrl+space')).toEqual({
      ch: 'space',
      mod: 'ctrl',
      named: 'space',
      raw: 'ctrl+space'
    })
    expect(parseVoiceRecordKey('alt+enter').named).toBe('enter')
    expect(parseVoiceRecordKey('alt+return').named).toBe('enter')
    expect(parseVoiceRecordKey('ctrl+tab').named).toBe('tab')
    expect(parseVoiceRecordKey('ctrl+escape').named).toBe('escape')
    expect(parseVoiceRecordKey('ctrl+esc').named).toBe('escape')
    expect(parseVoiceRecordKey('ctrl+backspace').named).toBe('backspace')
    expect(parseVoiceRecordKey('ctrl+delete').named).toBe('delete')
    expect(parseVoiceRecordKey('ctrl+del').named).toBe('delete')
  })

  it('falls back to Ctrl+B for unrecognised multi-character tokens', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')




    expect(parseVoiceRecordKey('ctrl+spcae')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+f5')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('does not throw on non-string YAML scalars — falls back instead', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')



    expect(parseVoiceRecordKey(1 as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey(true as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey(null as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey(undefined as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey({} as unknown as string)).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('rejects multi-modifier chords rather than silently dropping extras', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')




    expect(parseVoiceRecordKey('ctrl+alt+r')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('cmd+ctrl+b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('alt+ctrl+space')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('rejects bare-char configs without an explicit modifier', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')




    expect(parseVoiceRecordKey('o')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('space')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('escape')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('rejects ctrl+c / ctrl+d / ctrl+l — reserved by the TUI input handler', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')




    expect(parseVoiceRecordKey('ctrl+c')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+d')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+l')).toEqual(DEFAULT_VOICE_RECORD_KEY)


    expect(parseVoiceRecordKey('alt+c').mod).toBe('alt')



    expect(parseVoiceRecordKey('ctrl+x').mod).toBe('ctrl')
    expect(parseVoiceRecordKey('ctrl+x').ch).toBe('x')
  })

  it('rejects super+{c,d,l,v} on macOS — action-mod chords are claimed before voice', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('darwin')



    expect(parseVoiceRecordKey('super+c')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('super+d')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('super+l')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('super+v')).toEqual(DEFAULT_VOICE_RECORD_KEY)

    expect(parseVoiceRecordKey('super+b').mod).toBe('super')
    expect(parseVoiceRecordKey('super+o').mod).toBe('super')
  })

  it('allows super+{c,d,l,v} on Linux/Windows — those globals key off Ctrl, not Super', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')





    expect(parseVoiceRecordKey('super+c').mod).toBe('super')
    expect(parseVoiceRecordKey('super+d').mod).toBe('super')
    expect(parseVoiceRecordKey('super+l').mod).toBe('super')
    expect(parseVoiceRecordKey('super+v').mod).toBe('super')
  })

  it('rejects alt+{c,d,l} on macOS — meta-as-alt collides with isAction', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('darwin')






    expect(parseVoiceRecordKey('alt+c')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('alt+d')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('alt+l')).toEqual(DEFAULT_VOICE_RECORD_KEY)

    expect(parseVoiceRecordKey('alt+r').mod).toBe('alt')
    expect(parseVoiceRecordKey('alt+space').mod).toBe('alt')
  })

  it('allows alt+{c,d,l} on Linux/Windows — non-mac isAction keys off Ctrl', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')



    expect(parseVoiceRecordKey('alt+c').mod).toBe('alt')
    expect(parseVoiceRecordKey('alt+d').mod).toBe('alt')
    expect(parseVoiceRecordKey('alt+l').mod).toBe('alt')
  })

  it('super+<key> does NOT fire on key.meta-only events (Alt+X false-fire guard)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')





    const superB = parseVoiceRecordKey('super+b')
    const superSpace = parseVoiceRecordKey('super+space')
    const superEscape = parseVoiceRecordKey('super+escape')

    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', superB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, ' ', superSpace)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, escape: true, meta: true, super: false }, '', superEscape)).toBe(false)
  })

  it('default ctrl+b does NOT fire on Alt+B via isActionMod meta leak', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, isVoiceToggleKey } = await importPlatform('darwin')





    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(false)

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)
  })

  it('ctrl+<key> rejects chords with extra alt / meta / super bits', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const ctrlO = parseVoiceRecordKey('ctrl+o')





    expect(isVoiceToggleKey({ alt: true, ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: true, super: false }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: true }, 'o', ctrlO)).toBe(false)

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(true)
  })

  it('super+<key> rejects chords with extra ctrl / alt / meta bits', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const superB = parseVoiceRecordKey('super+b')

    expect(isVoiceToggleKey({ alt: true, ctrl: false, meta: false, super: true }, 'b', superB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: true }, 'b', superB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: true }, 'b', superB)).toBe(false)

    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', superB)).toBe(true)
  })

  it('alt+escape does not fire on bare Esc meta-shape', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const altEscape = parseVoiceRecordKey('alt+escape')


    expect(isVoiceToggleKey({ ctrl: false, escape: true, meta: true, super: false }, '', altEscape)).toBe(false)

    expect(isVoiceToggleKey({ alt: true, ctrl: false, escape: true, meta: false, super: false }, '', altEscape)).toBe(
      true
    )
  })

  it('rejects matches when Shift is held (different chord than configured)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')




    const ctrlTab = parseVoiceRecordKey('ctrl+tab')
    const altEnter = parseVoiceRecordKey('alt+enter')
    const ctrlO = parseVoiceRecordKey('ctrl+o')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, shift: true, super: false, tab: true }, '', ctrlTab)).toBe(false)
    expect(
      isVoiceToggleKey({ alt: true, ctrl: false, meta: false, return: true, shift: true, super: false }, '', altEnter)
    ).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, shift: true, super: false }, 'o', ctrlO)).toBe(false)


    expect(isVoiceToggleKey({ ctrl: true, meta: false, shift: false, super: false, tab: true }, '', ctrlTab)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, shift: false, super: false }, 'o', ctrlO)).toBe(true)
  })
})

describe('formatVoiceRecordKey', () => {
  it('renders as the user expects in /voice status', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('linux')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+b'))).toBe('Ctrl+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+o'))).toBe('Ctrl+O')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('alt+r'))).toBe('Alt+R')



    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+b'))).toBe('Super+B')
  })

  it('renders named keys in title case (Ctrl+Space, Ctrl+Enter)', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('linux')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+space'))).toBe('Ctrl+Space')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('alt+enter'))).toBe('Alt+Enter')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+esc'))).toBe('Ctrl+Escape')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+space'))).toBe('Super+Space')
  })
})

describe('isVoiceToggleKey honours configured record key', () => {
  it('binds the configured letter, not hardcoded b', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const ctrlO = parseVoiceRecordKey('ctrl+o')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(true)

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', ctrlO)).toBe(false)
  })

  it('alt+<letter> binding matches alt OR meta (terminal-protocol parity)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const altR = parseVoiceRecordKey('alt+r')

    expect(isVoiceToggleKey({ alt: true, ctrl: false, meta: false, super: false }, 'r', altR)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'r', altR)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: false }, 'r', altR)).toBe(false)
  })

  it('binds named keys via ink event flags (space → ch === " ", enter → key.return, …)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')

    const ctrlSpace = parseVoiceRecordKey('ctrl+space')
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, ' ', ctrlSpace)).toBe(true)

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', ctrlSpace)).toBe(false)

    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: false }, ' ', ctrlSpace)).toBe(false)

    const ctrlEnter = parseVoiceRecordKey('ctrl+enter')
    expect(isVoiceToggleKey({ ctrl: true, meta: false, return: true, super: false }, '', ctrlEnter)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, return: false, super: false }, '', ctrlEnter)).toBe(false)

    const altTab = parseVoiceRecordKey('alt+tab')
    expect(isVoiceToggleKey({ alt: true, ctrl: false, meta: false, super: false, tab: true }, '', altTab)).toBe(true)
    expect(isVoiceToggleKey({ alt: false, ctrl: false, meta: false, super: false, tab: true }, '', altTab)).toBe(false)

    const ctrlEscape = parseVoiceRecordKey('ctrl+escape')
    expect(isVoiceToggleKey({ ctrl: true, escape: true, meta: false, super: false }, '', ctrlEscape)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, escape: false, meta: false, super: false }, '', ctrlEscape)).toBe(false)

    const ctrlBackspace = parseVoiceRecordKey('ctrl+backspace')
    expect(isVoiceToggleKey({ backspace: true, ctrl: true, meta: false, super: false }, '', ctrlBackspace)).toBe(true)

    const ctrlDelete = parseVoiceRecordKey('ctrl+delete')
    expect(isVoiceToggleKey({ ctrl: true, delete: true, meta: false, super: false }, '', ctrlDelete)).toBe(true)
  })

  it('omitted configured key falls back to ctrl+b (back-compat)', async () => {
    const { isVoiceToggleKey } = await importPlatform('linux')


    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b')).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o')).toBe(false)
  })







  it('ctrl+escape does NOT fire on bare Esc via key.meta on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const ctrlEscape = parseVoiceRecordKey('ctrl+escape')


    expect(isVoiceToggleKey({ ctrl: false, escape: true, meta: true, super: false }, '', ctrlEscape)).toBe(false)

    expect(isVoiceToggleKey({ ctrl: true, escape: true, meta: false, super: false }, '', ctrlEscape)).toBe(true)
  })

  it('ctrl+space does NOT fire on Alt+Space on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const ctrlSpace = parseVoiceRecordKey('ctrl+space')


    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, ' ', ctrlSpace)).toBe(false)

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, ' ', ctrlSpace)).toBe(true)
  })

  it('default ctrl+b accepts raw Ctrl+B and kitty-style Cmd+B on macOS', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, isVoiceToggleKey } = await importPlatform('darwin')


    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)

    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)



    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(false)
  })

  it('custom ctrl+<letter> does NOT accept Cmd fallback on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const ctrlO = parseVoiceRecordKey('ctrl+o')



    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(true)
  })

  it('super+b renders "Cmd+B" on darwin and requires the literal key.super bit', async () => {
    const { formatVoiceRecordKey, isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const superB = parseVoiceRecordKey('super+b')

    expect(formatVoiceRecordKey(superB)).toBe('Cmd+B')

    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', superB)).toBe(true)


    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', superB)).toBe(false)

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: true }, 'b', superB)).toBe(false)
  })

  it('super+b renders "Super+B" on Linux (not "Cmd+B")', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('linux')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+b'))).toBe('Super+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('win+b'))).toBe('Super+B')
  })

  it('super+b still renders "Cmd+B" on macOS', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('darwin')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+b'))).toBe('Cmd+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('win+b'))).toBe('Cmd+B')
  })

  it('ctrl+b aliases (control+b, "ctrl + b") still accept Cmd+B fallback on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const controlB = parseVoiceRecordKey('control+b')
    const spacedB = parseVoiceRecordKey('ctrl + b')





    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', controlB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', spacedB)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', controlB)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', spacedB)).toBe(true)

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', controlB)).toBe(true)

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', controlB)).toBe(false)
  })
})

describe('isMacActionFallback', () => {
  it('routes raw Ctrl+K and Ctrl+W to readline kill-to-end / delete-word on macOS', async () => {
    const { isMacActionFallback } = await importPlatform('darwin')

    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'k', 'k')).toBe(true)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'w', 'w')).toBe(true)

    expect(isMacActionFallback({ ctrl: true, meta: true, super: false }, 'k', 'k')).toBe(false)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: true }, 'w', 'w')).toBe(false)
  })

  it('is a no-op on non-macOS (Linux routes Ctrl+K/W through isActionMod directly)', async () => {
    const { isMacActionFallback } = await importPlatform('linux')

    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'k', 'k')).toBe(false)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'w', 'w')).toBe(false)
  })
})
