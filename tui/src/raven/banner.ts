




const RICH_RE = /\[(?:bold\s+)?(?:dim\s+)?(#(?:[0-9a-fA-F]{3,8}))\]([\s\S]*?)(\[\/\])/g

export function parseRichMarkup(markup: string): Line[] {
  const lines: Line[] = []

  for (const raw of markup.split('\n')) {
    const trimmed = raw.trimEnd()

    if (!trimmed) {
      lines.push(['', ' '])

      continue
    }

    const matches = [...trimmed.matchAll(RICH_RE)]

    if (!matches.length) {
      lines.push(['', trimmed])

      continue
    }

    let cursor = 0

    for (const m of matches) {
      const before = trimmed.slice(cursor, m.index)

      if (before) {
        lines.push(['', before])
      }

      lines.push([m[1]!, m[2]!])
      cursor = m.index! + m[0].length
    }

    if (cursor < trimmed.length) {
      lines.push(['', trimmed.slice(cursor)])
    }
  }

  return lines
}

const RAVEN_LOGO_ART = [
  ' ██████████ ██████████ ███    ███  █████   ',
  '███░░░░░░░ ░░███░░░░░█░████  ████ ███░░███  ',
  ' ░███  ░   ░███  █ ░ ░███ ███ ███░███ ░███ ',
  ' ░████████  ░██████   ░███░░█ ░███░█████████',
  '  ░░░░░███  ░███░░█   ░███ ░  ░███░███░░███',
  '       ░███ ░███ ░   █░███    ░███░███ ░███',
  ' ██████████ ██████████ █████   █████████ █████',
  '░░░░░░░░░░ ░░░░░░░░░░ ░░░░░   ░░░░░░░░░ ░░░░░ '
] as const

const RAVEN_HERO_ART = [
  '⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣠⣠⡤⣤⢤⡤⣤⣠⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀',
  '⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⡴⠞⣋⣉⠀⠀⠀⠀⠀⠀⠀⠀⠉⠙⠓⠦⣄⡀⠀⠀⠀⠀⠀⠀⠀⠀',
  '⠀⠀⠀⠀⠀⠀⢀⡴⢛⣥⣶⡟⠋⠀⠠⢤⣤⣤⣄⣀⠀⠀⠀⠀⠀⠀⠈⠙⢶⣄⠀⠀⠀⠀⠀⠀',
  '⠀⠀⠀⠀⢀⡴⣫⣾⣿⡿⠃⠀⣀⣴⣶⣿⣿⣿⣿⠿⣷⣤⣤⣤⣀⠀⠀⠀⠀⠈⢳⡄⠀⠀⠀⠀',
  '⠀⠀⠀⣰⢏⣼⣿⣿⡿⠁⠀⢀⣩⣴⣾⣿⣿⣿⣿⣿⣿⣿⠿⠟⠛⠓⠀⠀⠀⠀⠀⠙⣦⠀⠀⠀',
  '⠀⠀⣰⢏⣾⣿⣿⣿⠁⠀⣰⠞⢋⣽⣿⣿⣿⣿⣿⣿⠏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⣧⠀⠀',
  '⠀⢰⡏⣾⣿⣿⣿⡏⠀⠈⣰⢊⣾⠟⣵⡿⣻⣽⣿⣿⣧⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⣇⠀',
  '⠀⣾⢸⣿⣿⣿⣿⡇⠀⣼⡯⢰⠃⢼⠋⣴⣿⣿⣿⣿⣿⣿⣷⣦⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻⡀',
  '⢐⡇⣿⣿⣿⣿⣿⣇⢀⣿⡇⠈⠀⠋⢀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣄⠀⠀⠀⠀⠀⠀⠀⠀⢸⡇',
  '⠰⡇⣿⣿⣿⣿⣿⣿⡘⣿⣗⠀⠀⠀⠀⢿⣿⣿⣷⣝⡻⢿⣿⣿⣿⣿⣧⡀⠀⠀⠀⠀⠀⠀⢸⡇',
  '⠈⣇⢿⣿⣿⣿⣿⣿⣷⣿⣿⡆⠀⠀⠀⠈⢿⣿⣿⣟⢿⣶⣬⣙⠻⢿⣿⣷⡀⠀⠀⠀⠀⠀⢸⡇',
  '⠀⢿⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠀⠀⠀⠈⠻⣿⣿⢷⣬⣙⠻⠿⣦⣝⢿⣷⡀⠀⠀⠀⠀⣼⠀',
  '⠀⠘⣇⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣆⠀⠀⠀⠀⠈⠿⣷⣝⡻⢿⣷⣮⣙⢷⣿⣧⠀⠀⠀⢰⠇⠀',
  '⠀⠀⠹⣎⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣄⠀⠀⠀⠀⠙⢿⣿⣷⣬⣙⠿⣷⣿⣿⡄⠀⢠⠟⠀⠀',
  '⠀⠀⠀⠙⣦⡻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣦⣀⠀⠀⠘⣿⡿⣿⣿⣷⣮⡻⣿⣇⢠⠋⠀⠀⠀',
  '⠀⠀⠀⠀⠈⠳⣌⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠆⣿⡏⣿⡝⣿⡽⣿⣌⢿⠀⠀⠀⠀⠀',
  '⠀⠀⠀⠀⠀⠀⠈⠳⢮⣛⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡟⣸⣿⠅⣿⡧⣻⣿⡘⣿⣦⠀⠀⠀⠀⠀',
  '⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠛⠮⣭⣛⠿⠿⣿⣿⣿⡿⣱⣿⡟⢨⣿⡯⢸⣿⣧⢹⣿⡆⠀⠀⠀⠀',
  '⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠉⠛⠒⠲⠒⠂⠻⠟⠁⣺⣿⡏⠘⣿⣿⠀⠙⠃⠀⠀⠀⠀',
  '⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠋⠀⠀⠀⠁⠀⠀⠀⠀⠀⠀⠀'
]

export const RAVEN_LOGO_WIDTH = RAVEN_LOGO_ART.reduce((m, r) => Math.max(m, [...r].length), 0)
export const RAVEN_HERO_WIDTH = 36


const LOGO_ROWS_PER_BAND = 2

const HERO_BANDS = 5

const bandColor = (ramp: readonly string[], row: number) =>
  ramp[Math.floor(row / LOGO_ROWS_PER_BAND)] ?? ramp[ramp.length - 1]!



export const ravenLogo = (ramp: readonly string[], customLogo?: string): Line[] =>
  customLogo ? parseRichMarkup(customLogo) : RAVEN_LOGO_ART.map((text, i) => [bandColor(ramp, i), text])


const leftAlign = (rows: readonly string[]): string[] => {
  let lead = Infinity

  for (const r of rows) {
    if (r.trim() === '') {
      continue
    }

    lead = Math.min(lead, r.length - r.trimStart().length)
  }

  return !Number.isFinite(lead) || lead === 0 ? [...rows] : rows.map(r => r.slice(lead))
}



const RAVEN_WORD = leftAlign(RAVEN_LOGO_ART)


export const RAVEN_WORD_WIDTH = RAVEN_WORD.reduce((m, r) => Math.max(m, [...r].length), 0)


export const ravenLogoWord = (ramp: readonly string[]): Line[] =>
  RAVEN_WORD.map((text, i) => [bandColor(ramp, i), text])





export const ravenHero = (ramp: readonly string[], customHero?: string): Line[][] => {
  if (customHero) {
    return parseRichMarkup(customHero).map(line => [line])
  }

  return RAVEN_HERO_ART.map(row => {
    const chars = [...row]
    const bandWidth = Math.ceil(chars.length / HERO_BANDS)
    const segments: Line[] = []

    for (let band = 0; band < HERO_BANDS; band++) {
      const text = chars.slice(band * bandWidth, (band + 1) * bandWidth).join('')

      if (!text) {
        continue
      }

      const color = ramp[HERO_BANDS - 1 - band] ?? ramp[ramp.length - 1]!
      segments.push([color, text])
    }

    return segments
  })
}

export const artWidth = (lines: Line[]) => lines.reduce((m, [, t]) => Math.max(m, [...t].length), 0)


export const rowsWidth = (rows: Line[][]) =>
  rows.reduce(
    (m, segs) =>
      Math.max(
        m,
        segs.reduce((a, [, t]) => a + [...t].length, 0)
      ),
    0
  )

type Line = [string, string]
