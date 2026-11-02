




import { activeColorTier } from '@hermes/ink'

export interface ThemeColors {
  primary: string
  accent: string
  border: string
  text: string
  muted: string
  completionBg: string
  completionCurrentBg: string
  completionMetaBg: string
  completionMetaCurrentBg: string

  label: string
  ok: string
  error: string
  warn: string

  prompt: string
  sessionLabel: string
  sessionBorder: string

  statusBg: string
  statusFg: string
  statusGood: string
  statusWarn: string
  statusBad: string
  statusCritical: string
  selectionBg: string

  diffAdded: string
  diffRemoved: string
  diffAddedWord: string
  diffRemovedWord: string

  shellDollar: string
}

export interface ThemeBrand {
  name: string
  icon: string
  prompt: string
  welcome: string
  goodbye: string
  tool: string
  helpHeader: string
}

export interface Theme {
  color: ThemeColors
  brand: ThemeBrand
  bannerLogo: string
  bannerHero: string


  yellow: readonly string[]
}

export type ColorScheme = 'dark' | 'light'









function parseHex(h: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(h)

  if (!m) {
    return null
  }

  const n = parseInt(m[1]!, 16)

  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff]
}

function mix(a: string, b: string, t: number) {
  const pa = parseHex(a)
  const pb = parseHex(b)

  if (!pa || !pb) {
    return a
  }

  const lerp = (i: 0 | 1 | 2) => Math.round(pa[i] + (pb[i] - pa[i]) * t)

  return '#' + ((1 << 24) | (lerp(0) << 16) | (lerp(1) << 8) | lerp(2)).toString(16).slice(1)
}



const BRAND: ThemeBrand = {
  name: 'Sema',
  icon: 'S',
  prompt: '❯',
  welcome: 'Type your message or /help for commands.',
  goodbye: 'Goodbye.',
  tool: '┊',
  helpHeader: 'Sema Commands'
}

const cleanPromptSymbol = (s: string | undefined, fallback: string) => {
  const cleaned = String(s ?? '')
    .replace(/\s+/g, ' ')
    .trim()

  return cleaned || fallback
}















const YELLOW_RAMP_TC_DARK: readonly string[] = [
  '#c8fbff', // 50
  '#8ff7ff', // 100
  '#4eefff', // 300
  '#20d8ff', // 500
  '#00aee6', // 600
  '#007db8', // 700
  '#00527c', // 900
  '#00324d', // 950
  '#001b2b'
]

const YELLOW_RAMP_TC_LIGHT: readonly string[] = [
  '#b7f8ff',
  '#79e7ff',
  '#27c7f2',
  '#008cbd', // 500
  '#006f99', // 600
  '#006f99', // 700
  '#004d70',
  '#00344d',
  '#001d2e'
]

const YELLOW_RAMP_256_DARK: readonly string[] = [
  'ansi256(159)',
  'ansi256(123)',
  'ansi256(87)',
  'ansi256(45)', // 500
  'ansi256(39)',
  'ansi256(31)',
  'ansi256(24)',
  'ansi256(17)'
]

const YELLOW_RAMP_256_LIGHT: readonly string[] = [
  'ansi256(159)',
  'ansi256(123)',
  'ansi256(38)',
  'ansi256(31)',
  'ansi256(24)',
  'ansi256(24)',
  'ansi256(17)',
  'ansi256(16)'
]

const YELLOW_RAMP_16: readonly string[] = [
  'ansi:cyanBright',
  'ansi:cyanBright',
  'ansi:cyan',
  'ansi:cyan',
  'ansi:blueBright',
  'ansi:blueBright',
  'ansi:blue',
  'ansi:blue'
]

function yellowRamp(tier: 0 | 1 | 2 | 3, scheme: ColorScheme): readonly string[] {
  if (tier === 2) {
    return scheme === 'light' ? YELLOW_RAMP_256_LIGHT : YELLOW_RAMP_256_DARK
  }
  if (tier === 1) {
    return YELLOW_RAMP_16
  }
  return scheme === 'light' ? YELLOW_RAMP_TC_LIGHT : YELLOW_RAMP_TC_DARK
}



export const DARK_THEME: Theme = {
  color: {
    primary: '#20d8ff',
    accent: '#20d8ff',
    border: '#1c3f4d',
    text: '#FFF5EA',
    muted: '#858482',
    completionBg: '#000000',
    completionCurrentBg: '#082633',
    completionMetaBg: '#080808',
    completionMetaCurrentBg: '#071f2a',

    label: '#858482',
    ok: '#3ee07a',
    error: '#ec6a5e',
    warn: '#f5a623',

    prompt: '#20d8ff',
    sessionLabel: '#858482',
    sessionBorder: '#1c3f4d',

    statusBg: '#000000',
    statusFg: '#999999',
    statusGood: '#3ee07a',
    statusWarn: '#f5a623',
    statusBad: '#ec6a5e',
    statusCritical: '#ec6a5e',
    selectionBg: '#00527c',

    diffAdded: '#13260f',
    diffRemoved: '#2a1416',
    diffAddedWord: '#86c957',
    diffRemovedWord: '#f85149',
    shellDollar: '#20d8ff'
  },

  brand: BRAND,

  bannerLogo: '',
  bannerHero: '',
  yellow: YELLOW_RAMP_TC_DARK
}




export const LIGHT_THEME: Theme = {
  color: {
    primary: '#008cbd',
    accent: '#008cbd',
    border: '#d0d7de',
    text: '#24201a',
    muted: '#57606a',
    completionBg: '#f6f8fa',
    completionCurrentBg: '#e2f7ff',
    completionMetaBg: '#eef1f4',
    completionMetaCurrentBg: '#ccefff',

    label: '#6e7681',
    ok: '#1f7a33',
    error: '#cf222e',
    warn: '#a05500',

    prompt: '#008cbd',
    sessionLabel: '#6e7681',
    sessionBorder: '#d0d7de',

    statusBg: '#f6f8fa',
    statusFg: '#57606a',
    statusGood: '#1f7a33',
    statusWarn: '#a05500',
    statusBad: '#cf222e',
    statusCritical: '#cf222e',
    selectionBg: '#aeefff',

    diffAdded: '#e6f7dd',
    diffRemoved: '#ffe3e0',
    diffAddedWord: '#3f6f1f',
    diffRemovedWord: '#c0282f',
    shellDollar: '#008cbd'
  },

  brand: BRAND,

  bannerLogo: '',
  bannerHero: '',
  yellow: YELLOW_RAMP_TC_LIGHT
}



const DARK_256_COLORS: ThemeColors = {
  primary: 'ansi256(45)',
  accent: 'ansi256(45)',
  border: 'ansi256(24)',
  text: 'ansi256(255)',
  muted: 'ansi256(102)',
  completionBg: 'ansi256(16)',
  completionCurrentBg: 'ansi256(17)',
  completionMetaBg: 'ansi256(232)',
  completionMetaCurrentBg: 'ansi256(234)',
  label: 'ansi256(102)',
  ok: 'ansi256(78)',
  error: 'ansi256(203)',
  warn: 'ansi256(214)',
  prompt: 'ansi256(45)',
  sessionLabel: 'ansi256(102)',
  sessionBorder: 'ansi256(24)',
  statusBg: 'ansi256(16)',
  statusFg: 'ansi256(246)',
  statusGood: 'ansi256(78)',
  statusWarn: 'ansi256(214)',
  statusBad: 'ansi256(203)',
  statusCritical: 'ansi256(203)',
  selectionBg: 'ansi256(24)',
  diffAdded: 'ansi256(234)',
  diffRemoved: 'ansi256(234)',
  diffAddedWord: 'ansi256(107)',
  diffRemovedWord: 'ansi256(203)',
  shellDollar: 'ansi256(45)'
}

const LIGHT_256_COLORS: ThemeColors = {
  primary: 'ansi256(31)',
  accent: 'ansi256(31)',
  border: 'ansi256(188)',
  text: 'ansi256(234)',
  muted: 'ansi256(59)',
  completionBg: 'ansi256(231)',
  completionCurrentBg: 'ansi256(195)',
  completionMetaBg: 'ansi256(255)',
  completionMetaCurrentBg: 'ansi256(195)',
  label: 'ansi256(243)',
  ok: 'ansi256(29)',
  error: 'ansi256(160)',
  warn: 'ansi256(130)',
  prompt: 'ansi256(31)',
  sessionLabel: 'ansi256(243)',
  sessionBorder: 'ansi256(188)',
  statusBg: 'ansi256(231)',
  statusFg: 'ansi256(59)',
  statusGood: 'ansi256(29)',
  statusWarn: 'ansi256(130)',
  statusBad: 'ansi256(160)',
  statusCritical: 'ansi256(160)',
  selectionBg: 'ansi256(153)',
  diffAdded: 'ansi256(194)',
  diffRemoved: 'ansi256(224)',
  diffAddedWord: 'ansi256(64)',
  diffRemovedWord: 'ansi256(124)',
  shellDollar: 'ansi256(136)'
}









const DARK_16_COLORS: ThemeColors = {
  primary: 'ansi:cyanBright',
  accent: 'ansi:cyanBright',
  border: 'ansi:blackBright',
  text: 'ansi:white',
  muted: 'ansi:blackBright',
  completionBg: 'ansi:black',
  completionCurrentBg: 'ansi:blackBright',
  completionMetaBg: 'ansi:black',
  completionMetaCurrentBg: 'ansi:blackBright',
  label: 'ansi:blackBright',
  ok: 'ansi:greenBright',
  error: 'ansi:redBright',
  warn: 'ansi:yellow',
  prompt: 'ansi:cyanBright',
  sessionLabel: 'ansi:blackBright',
  sessionBorder: 'ansi:blackBright',
  statusBg: 'ansi:black',
  statusFg: 'ansi:blackBright',
  statusGood: 'ansi:greenBright',
  statusWarn: 'ansi:yellow',
  statusBad: 'ansi:redBright',
  statusCritical: 'ansi:red',
  selectionBg: 'ansi:blue',
  diffAdded: 'ansi:blackBright',
  diffRemoved: 'ansi:blackBright',
  diffAddedWord: 'ansi:green',
  diffRemovedWord: 'ansi:red',
  shellDollar: 'ansi:cyanBright'
}

const LIGHT_16_COLORS: ThemeColors = {
  primary: 'ansi:cyan',
  accent: 'ansi:cyan',
  border: 'ansi:blackBright',
  text: 'ansi:black',
  muted: 'ansi:blackBright',
  completionBg: 'ansi:white',
  completionCurrentBg: 'ansi:blackBright',
  completionMetaBg: 'ansi:white',
  completionMetaCurrentBg: 'ansi:blackBright',
  label: 'ansi:blackBright',
  ok: 'ansi:green',
  error: 'ansi:red',
  warn: 'ansi:yellow',
  prompt: 'ansi:cyan',
  sessionLabel: 'ansi:blackBright',
  sessionBorder: 'ansi:blackBright',
  statusBg: 'ansi:white',
  statusFg: 'ansi:blackBright',
  statusGood: 'ansi:green',
  statusWarn: 'ansi:yellow',
  statusBad: 'ansi:red',
  statusCritical: 'ansi:red',
  selectionBg: 'ansi:magentaBright',
  diffAdded: 'ansi:blackBright',
  diffRemoved: 'ansi:blackBright',
  diffAddedWord: 'ansi:green',
  diffRemovedWord: 'ansi:red',
  shellDollar: 'ansi:cyan'
}

const DARK_256: Theme = { ...DARK_THEME, color: DARK_256_COLORS, yellow: YELLOW_RAMP_256_DARK }
const DARK_16: Theme = { ...DARK_THEME, color: DARK_16_COLORS, yellow: YELLOW_RAMP_16 }
const LIGHT_256: Theme = { ...LIGHT_THEME, color: LIGHT_256_COLORS, yellow: YELLOW_RAMP_256_LIGHT }
const LIGHT_16: Theme = { ...LIGHT_THEME, color: LIGHT_16_COLORS, yellow: YELLOW_RAMP_16 }






export function resolveTheme(scheme: ColorScheme, tier: 0 | 1 | 2 | 3): Theme {
  if (scheme === 'light') {
    if (tier === 2) {
      return LIGHT_256
    }
    if (tier === 1) {
      return LIGHT_16
    }
    return LIGHT_THEME
  }

  if (tier === 2) {
    return DARK_256
  }
  if (tier === 1) {
    return DARK_16
  }
  return DARK_THEME
}



const TRUE_RE = /^(?:1|true|yes|on)$/
const FALSE_RE = /^(?:0|false|no|off)$/








const LIGHT_DEFAULT_TERM_PROGRAMS = new Set<string>([])






const LUMA_LIGHT_THRESHOLD = 0.6





const HEX_3_RE = /^[0-9a-f]{3}$/
const HEX_6_RE = /^[0-9a-f]{6}$/

function backgroundLuminance(raw: string): null | number {
  const v = raw.trim().toLowerCase()

  if (!v) {
    return null
  }

  const hex = v.startsWith('#') ? v.slice(1) : v

  const rgb = HEX_6_RE.test(hex)
    ? [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)]
    : HEX_3_RE.test(hex)
      ? [parseInt(hex[0]! + hex[0]!, 16), parseInt(hex[1]! + hex[1]!, 16), parseInt(hex[2]! + hex[2]!, 16)]
      : null

  if (!rgb) {
    return null
  }


  return (0.2126 * rgb[0]! + 0.7152 * rgb[1]! + 0.0722 * rgb[2]!) / 255
}



















export function detectLightMode(
  env: NodeJS.ProcessEnv = process.env,


  lightDefaultTermPrograms: ReadonlySet<string> = LIGHT_DEFAULT_TERM_PROGRAMS
): boolean {
  const lightFlag = (env.RAVEN_TUI_LIGHT ?? '').trim().toLowerCase()

  if (TRUE_RE.test(lightFlag)) {
    return true
  }

  if (FALSE_RE.test(lightFlag)) {
    return false
  }

  const themeFlag = (env.RAVEN_TUI_THEME ?? '').trim().toLowerCase()

  if (themeFlag === 'light') {
    return true
  }

  if (themeFlag === 'dark') {
    return false
  }

  const bgHint = backgroundLuminance(env.RAVEN_TUI_BACKGROUND ?? '')

  if (bgHint !== null) {
    return bgHint >= LUMA_LIGHT_THRESHOLD
  }

  const colorfgbg = (env.COLORFGBG ?? '').trim()

  if (colorfgbg) {




    const lastField = colorfgbg.split(';').at(-1) ?? ''

    if (/^\d+$/.test(lastField)) {
      const bg = Number(lastField)

      if (bg === 7 || bg === 15) {
        return true
      }




      if (bg >= 0 && bg < 16) {
        return false
      }
    }
  }

  const termProgram = (env.TERM_PROGRAM ?? '').trim()

  return lightDefaultTermPrograms.has(termProgram)
}

const DEFAULT_LIGHT_MODE = detectLightMode()
const DEFAULT_SCHEME: ColorScheme = DEFAULT_LIGHT_MODE ? 'light' : 'dark'

export const DEFAULT_THEME: Theme = resolveTheme(DEFAULT_SCHEME, activeColorTier())





let detectedScheme: ColorScheme | null = null




export function currentScheme(): ColorScheme {
  return detectedScheme ?? DEFAULT_SCHEME
}




export function resolveCurrentDefaultTheme(): Theme {
  return resolveTheme(currentScheme(), activeColorTier())
}





export function cursorColorHex(theme: Theme): string {
  const p = theme.color.primary

  return p.startsWith('#') ? p : currentScheme() === 'light' ? LIGHT_THEME.color.primary : DARK_THEME.color.primary
}





function oscColorToHex(data: string): null | string {
  const s = data.trim().toLowerCase()
  const m = /^rgba?:([0-9a-f]{1,4})\/([0-9a-f]{1,4})\/([0-9a-f]{1,4})/.exec(s)

  if (m) {
    const scale = (h: string) => Math.round((parseInt(h, 16) / (16 ** h.length - 1)) * 255)

    return '#' + [m[1]!, m[2]!, m[3]!].map(h => scale(h).toString(16).padStart(2, '0')).join('')
  }

  const hex = s.startsWith('#') ? s.slice(1) : s

  if (HEX_6_RE.test(hex)) {
    return '#' + hex
  }

  if (HEX_3_RE.test(hex)) {
    return '#' + [...hex].map(c => c + c).join('')
  }

  return null
}











export function applyDetectedBackground(oscData: string): { changed: boolean; scheme: ColorScheme } | null {
  const hex = oscColorToHex(oscData)

  if (!hex) {
    return null
  }

  process.env.RAVEN_TUI_BACKGROUND = hex
  const scheme: ColorScheme = detectLightMode() ? 'light' : 'dark'
  const changed = scheme !== currentScheme()
  detectedScheme = scheme

  return { changed, scheme }
}



function skinColors(colors: Record<string, string>): ThemeColors {
  const base = (currentScheme() === 'light' ? LIGHT_THEME : DARK_THEME).color
  const c = (k: string) => colors[k]
  const hasSkinColors = Object.keys(colors).length > 0

  const accent = c('ui_accent') ?? c('banner_accent') ?? base.accent
  const bannerAccent = c('banner_accent') ?? c('banner_title') ?? base.accent
  const muted = c('banner_dim') ?? base.muted
  const completionBg = c('completion_menu_bg') ?? base.completionBg

  const completionCurrentBg =
    c('completion_menu_current_bg') ??
    (hasSkinColors ? mix(completionBg, bannerAccent, 0.25) : base.completionCurrentBg)





  const completionMetaBg = c('completion_menu_meta_bg') ?? c('completion_menu_bg') ?? base.completionMetaBg
  const completionMetaCurrentBg =
    c('completion_menu_meta_current_bg') ??
    c('completion_menu_current_bg') ??
    (hasSkinColors ? completionCurrentBg : base.completionMetaCurrentBg)

  return {
    primary: c('ui_primary') ?? c('banner_title') ?? base.primary,
    accent,
    border: c('ui_border') ?? c('banner_border') ?? base.border,
    text: c('ui_text') ?? c('banner_text') ?? base.text,
    muted,
    completionBg,
    completionCurrentBg,
    completionMetaBg,
    completionMetaCurrentBg,

    label: c('ui_label') ?? base.label,
    ok: c('ui_ok') ?? base.ok,
    error: c('ui_error') ?? base.error,
    warn: c('ui_warn') ?? base.warn,

    prompt: c('prompt') ?? c('banner_text') ?? base.prompt,
    sessionLabel: c('session_label') ?? base.sessionLabel,
    sessionBorder: c('session_border') ?? base.sessionBorder,

    statusBg: base.statusBg,
    statusFg: base.statusFg,
    statusGood: c('ui_ok') ?? base.statusGood,
    statusWarn: c('ui_warn') ?? base.statusWarn,
    statusBad: base.statusBad,
    statusCritical: base.statusCritical,
    selectionBg:
      c('selection_bg') ?? c('completion_menu_current_bg') ?? (hasSkinColors ? completionCurrentBg : base.selectionBg),

    diffAdded: base.diffAdded,
    diffRemoved: base.diffRemoved,
    diffAddedWord: base.diffAddedWord,
    diffRemovedWord: base.diffRemovedWord,
    shellDollar: c('shell_dollar') ?? base.shellDollar
  }
}

export function fromSkin(
  colors: Record<string, string>,
  branding: Record<string, string>,
  bannerLogo = '',
  bannerHero = '',
  toolPrefix = '',
  helpHeader = ''
): Theme {
  const d = DEFAULT_THEME

  const brand: ThemeBrand = {
    name: branding.agent_name ?? d.brand.name,
    icon: d.brand.icon,
    prompt: cleanPromptSymbol(branding.prompt_symbol, d.brand.prompt),
    welcome: branding.welcome ?? d.brand.welcome,
    goodbye: branding.goodbye ?? d.brand.goodbye,
    tool: toolPrefix || d.brand.tool,
    helpHeader: branding.help_header ?? (helpHeader || d.brand.helpHeader)
  }





  const tier = activeColorTier()
  const color = tier === 1 || tier === 2 ? resolveTheme(currentScheme(), tier).color : skinColors(colors)

  return { color, brand, bannerLogo, bannerHero, yellow: yellowRamp(tier, currentScheme()) }
}
