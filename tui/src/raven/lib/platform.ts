















export const isMac = process.platform === 'darwin'


export const isActionMod = (key: { ctrl: boolean; meta: boolean; super?: boolean }): boolean =>
  isMac ? key.meta || key.super === true : key.ctrl










export const isMacActionFallback = (
  key: { ctrl: boolean; meta: boolean; super?: boolean },
  ch: string,
  target: 'a' | 'e' | 'u' | 'k' | 'w'
): boolean => isMac && key.ctrl && !key.meta && key.super !== true && ch.toLowerCase() === target


export const isAction = (key: { ctrl: boolean; meta: boolean; super?: boolean }, ch: string, target: string): boolean =>
  isActionMod(key) && ch.toLowerCase() === target

export const isRemoteShell = (env: NodeJS.ProcessEnv = process.env): boolean =>
  Boolean(env.SSH_CONNECTION || env.SSH_CLIENT || env.SSH_TTY)

export const isCopyShortcut = (
  key: { ctrl: boolean; meta: boolean; super?: boolean },
  ch: string,
  env: NodeJS.ProcessEnv = process.env
): boolean =>
  ch.toLowerCase() === 'c' &&
  (isAction(key, ch, 'c') ||
    (isRemoteShell(env) && (key.meta || key.super === true)) ||



    (isMac && key.ctrl && (key.meta || key.super === true)))













export type VoiceRecordKeyMod = 'alt' | 'ctrl' | 'super'




export type VoiceRecordKeyNamed = 'backspace' | 'delete' | 'enter' | 'escape' | 'space' | 'tab'

export interface ParsedVoiceRecordKey {



  ch: string
  mod: VoiceRecordKeyMod
  named?: VoiceRecordKeyNamed
  raw: string
}

export const DEFAULT_VOICE_RECORD_KEY: ParsedVoiceRecordKey = {
  ch: 'b',
  mod: 'ctrl',
  raw: 'ctrl+b'
}



















const _MOD_ALIASES: Record<string, VoiceRecordKeyMod> = {
  alt: 'alt',
  control: 'ctrl',
  ctrl: 'ctrl',
  option: 'alt',
  opt: 'alt',
  super: 'super',
  win: 'super',
  windows: 'super'
}






const _NAMED_KEY_ALIASES: Record<string, VoiceRecordKeyNamed> = {
  backspace: 'backspace',
  bs: 'backspace',
  del: 'delete',
  delete: 'delete',
  enter: 'enter',
  esc: 'escape',
  escape: 'escape',
  ret: 'enter',
  return: 'enter',
  space: 'space',
  spc: 'space',
  tab: 'tab'
}











const _RESERVED_CTRL_CHARS = new Set(['c', 'd', 'l'])











const _RESERVED_SUPER_CHARS = new Set(['c', 'd', 'l', 'v'])







const _RESERVED_ALT_CHARS_MAC = new Set(['c', 'd', 'l'])

interface RuntimeKeyEvent {
  alt?: boolean
  backspace?: boolean
  ctrl: boolean
  delete?: boolean
  escape?: boolean
  meta: boolean
  return?: boolean
  shift?: boolean
  super?: boolean
  tab?: boolean
}




const _matchesNamedKey = (named: VoiceRecordKeyNamed, key: RuntimeKeyEvent, ch: string): boolean => {
  switch (named) {
    case 'backspace':
      return key.backspace === true

    case 'delete':
      return key.delete === true

    case 'enter':
      return key.return === true

    case 'escape':
      return key.escape === true

    case 'space':
      return ch === ' '

    case 'tab':
      return key.tab === true
  }
}















export const parseVoiceRecordKey = (raw: unknown): ParsedVoiceRecordKey => {
  if (typeof raw !== 'string') {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const lower = raw.trim().toLowerCase()

  if (!lower) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const parts = lower
    .split('+')
    .map(p => p.trim())
    .filter(Boolean)

  if (!parts.length) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const last = parts[parts.length - 1]
  const modCandidates = parts.slice(0, -1)







  if (modCandidates.length > 1) {
    return DEFAULT_VOICE_RECORD_KEY
  }





  if (modCandidates.length === 0) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const norm = _MOD_ALIASES[modCandidates[0]]




  if (!norm) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const mod = norm




  if (mod === 'ctrl' && last.length === 1 && _RESERVED_CTRL_CHARS.has(last)) {
    return DEFAULT_VOICE_RECORD_KEY
  }







  if (isMac && mod === 'super' && last.length === 1 && _RESERVED_SUPER_CHARS.has(last)) {
    return DEFAULT_VOICE_RECORD_KEY
  }






  if (isMac && mod === 'alt' && last.length === 1 && _RESERVED_ALT_CHARS_MAC.has(last)) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  if (last.length === 1) {
    return { ch: last, mod, raw: lower }
  }

  const named = _NAMED_KEY_ALIASES[last]

  if (named) {
    return { ch: named, mod, named, raw: lower }
  }



  return DEFAULT_VOICE_RECORD_KEY
}






export const formatVoiceRecordKey = (parsed: ParsedVoiceRecordKey): string => {
  const modLabel =
    parsed.mod === 'super' ? (isMac ? 'Cmd' : 'Super') : parsed.mod[0].toUpperCase() + parsed.mod.slice(1)



  const keyLabel = parsed.named ? parsed.named[0].toUpperCase() + parsed.named.slice(1) : parsed.ch.toUpperCase()

  return `${modLabel}+${keyLabel}`
}






const _isDefaultVoiceKey = (parsed: ParsedVoiceRecordKey): boolean =>
  parsed.mod === DEFAULT_VOICE_RECORD_KEY.mod &&
  parsed.ch === DEFAULT_VOICE_RECORD_KEY.ch &&
  parsed.named === DEFAULT_VOICE_RECORD_KEY.named

export const isVoiceToggleKey = (
  key: RuntimeKeyEvent,
  ch: string,
  configured: ParsedVoiceRecordKey = DEFAULT_VOICE_RECORD_KEY
): boolean => {



  if (configured.named) {
    if (!_matchesNamedKey(configured.named, key, ch)) {
      return false
    }
  } else if (ch.toLowerCase() !== configured.ch) {
    return false
  }





  if (key.shift === true) {
    return false
  }

  switch (configured.mod) {
    case 'alt':









      return (key.alt === true || (key.meta && key.escape !== true)) && !key.ctrl && key.super !== true

    case 'ctrl':









      if (key.ctrl) {
        return !key.alt && !key.meta && key.super !== true
      }

      return _isDefaultVoiceKey(configured) && isMac && key.super === true && !key.alt && !key.meta

    case 'super':






      return key.super === true && !key.ctrl && !key.alt && !key.meta
  }
}
