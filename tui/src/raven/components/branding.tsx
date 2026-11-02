




import { Box, Text, useStdout } from '@hermes/ink'
import { useEffect, useState } from 'react'
import unicodeSpinners from 'unicode-animations'

import type { PanelSection, SessionInfo } from '../types.js'

import { RAVEN_LOGO_WIDTH, ravenLogo, ravenLogoWord, RAVEN_WORD_WIDTH } from '../banner.js'
import { DEFAULT_THEME, type Theme } from '../theme.js'

const LOADER_TICK_MS = 120

function InlineLoader({ label, t }: { label: string; t: Theme }) {
  const [tick, setTick] = useState(0)
  const spinner = unicodeSpinners.braille
  const frame = spinner.frames[tick % spinner.frames.length] ?? '⠋'

  useEffect(() => {
    const id = setInterval(() => setTick(n => n + 1), Math.max(LOADER_TICK_MS, spinner.interval))

    return () => clearInterval(id)
  }, [spinner.interval])

  return (
    <Text color={t.color.muted} wrap="truncate">
      <Text color={t.color.accent}>{frame}</Text> {label}
    </Text>
  )
}

const STARTUP_MESSAGES = ['starting Sema...', 'connecting bridge...', 'loading session...']
const STARTUP_LABEL_MS = 900





export function StartupLoader({ t }: { t: Theme }) {
  const [step, setStep] = useState(0)

  useEffect(() => {
    const id = setInterval(() => setStep(n => n + 1), STARTUP_LABEL_MS)

    return () => clearInterval(id)
  }, [])

  const label = STARTUP_MESSAGES[Math.min(step, STARTUP_MESSAGES.length - 1)] ?? STARTUP_MESSAGES[0]

  return (
    <Box borderColor={t.color.border} borderStyle="round" marginBottom={1} paddingX={2} paddingY={1}>
      <InlineLoader label={label} t={t} />
    </Box>
  )
}

export function ArtLines({ lines }: { lines: [string, string][] }) {
  return (
    <>
      {lines.map(([c, text], i) => (


        <Text color={c} key={i} wrap="truncate">
          {text}
        </Text>
      ))}
    </>
  )
}



export function ArtRows({ rows }: { rows: [string, string][][] }) {
  return (
    <>
      {rows.map((segs, i) => (
        <Text key={i}>
          {segs.map(([c, text], j) => (
            <Text color={c} key={j}>
              {text}
            </Text>
          ))}
        </Text>
      ))}
    </>
  )
}

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  openrouter: 'OpenRouter',
  qwen: 'Qwen',
  google: 'Google',
  mistral: 'Mistral'
}











export function formatProvider(slug?: string, modelId?: string): string {
  let effective = slug ?? ''
  if (!effective || effective === 'auto') {



    const id = modelId ?? ''
    effective = id.includes('/') ? (id.split('/')[0] ?? '') : ''
  }
  if (!effective) {
    return '—'
  }
  const key = effective.toLowerCase()
  return PROVIDER_LABELS[key] ?? effective.charAt(0).toUpperCase() + effective.slice(1)
}




const LOGO_FULL_MIN_COLS = RAVEN_LOGO_WIDTH + 12


const LOGO_WORD_MIN_COLS = RAVEN_WORD_WIDTH + 5

export function Branding({ t }: { t?: Theme } = {}) {
  const theme = t ?? DEFAULT_THEME
  const yellow = theme.yellow


  const palette = [yellow[1], yellow[2], yellow[3], yellow[4]]
  const cols = useStdout().stdout?.columns ?? 80




  if (cols >= LOGO_FULL_MIN_COLS) {
    return (
      <Box flexDirection="column" marginBottom={1}>
        <ArtLines lines={ravenLogo(palette)} />
      </Box>
    )
  }

  if (cols >= LOGO_WORD_MIN_COLS) {
    return (
      <Box flexDirection="column" marginBottom={1}>
        <ArtLines lines={ravenLogoWord(palette)} />
      </Box>
    )
  }

  return (
    <Box marginBottom={1}>
      <Text bold color={theme.color.primary}>
        {theme.brand.icon} {theme.brand.name}
      </Text>
    </Box>
  )
}


export const Banner = Branding



export function SessionPanel({ info, maxCols, t }: SessionPanelProps) {



  const stdoutCols = useStdout().stdout?.columns ?? 100
  const cols = maxCols ?? stdoutCols
  const available = Math.max(20, cols - 12)
  const w = Math.min(available, 72)
  const rows: Array<[string, string]> = [
    ['Model', info.model],
    ['CWD', info.cwd || process.cwd()],
    ['Commands', '/help']
  ]

  return (
    <Box borderColor={t.color.border} borderStyle="round" flexDirection="column" marginBottom={1} paddingX={2} paddingY={1} width={w}>
      {rows.map(([label, value]) => (
        <Text key={label} wrap="truncate-end">
          <Text color={t.color.accent}>{label}: </Text>
          <Text color={label === 'Commands' ? t.color.accent : t.color.text}>
            {value}
          </Text>
        </Text>
      ))}
    </Box>
  )
}

export function Panel({ sections, t, title }: PanelProps) {
  return (
    <Box borderColor={t.color.border} borderStyle="round" flexDirection="column" paddingX={2} paddingY={1}>
      <Box justifyContent="center" marginBottom={1}>
        <Text bold color={t.color.primary}>
          {title}
        </Text>
      </Box>

      {sections.map((sec, si) => (
        <Box flexDirection="column" key={si} marginTop={si > 0 ? 1 : 0}>
          {sec.title && (
            <Text bold color={t.color.accent}>
              {sec.title}
            </Text>
          )}

          {sec.rows?.map(([k, v], ri) => (
            <Text key={ri} wrap="truncate">
              <Text color={t.color.muted}>{k.padEnd(20)}</Text>
              <Text color={t.color.text}>{v}</Text>
            </Text>
          ))}

          {sec.items?.map((item, ii) => (
            <Text color={t.color.text} key={ii} wrap="truncate">
              {item}
            </Text>
          ))}

          {sec.text && <Text color={t.color.muted}>{sec.text}</Text>}
        </Box>
      ))}
    </Box>
  )
}

interface PanelProps {
  sections: PanelSection[]
  t: Theme
  title: string
}

interface SessionPanelProps {
  info: SessionInfo


  maxCols?: number
  sid?: string | null
  t: Theme
}
