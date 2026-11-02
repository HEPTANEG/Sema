

















import type { FrameEvent } from '@hermes/ink'

import { scrollFastPathStats } from '@hermes/ink'
import { appendFileSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { Profiler, type ProfilerOnRenderCallback, type ReactNode } from 'react'

const ENABLED = /^(?:1|true|yes|on)$/i.test((process.env.RAVEN_DEV_PERF ?? '').trim())
const THRESHOLD_MS = Number(process.env.RAVEN_DEV_PERF_MS ?? '2') || 0
const LOG_PATH = process.env.RAVEN_DEV_PERF_LOG?.trim() || join(homedir(), '.raven', 'perf.log')

let logReady = false

const writeRow = (row: Record<string, unknown>) => {
  if (!logReady) {
    logReady = true

    try {
      mkdirSync(dirname(LOG_PATH), { recursive: true })
    } catch {
      // Best-effort — never crash the TUI to log a sample.
    }
  }

  try {
    appendFileSync(LOG_PATH, `${JSON.stringify(row)}\n`)
  } catch {
    /* best-effort */
  }
}

const round2 = (n: number) => Math.round(n * 100) / 100

const onRender: ProfilerOnRenderCallback = (id, phase, actualMs, baseMs, startTime, commitTime) => {
  if (actualMs < THRESHOLD_MS) {
    return
  }

  writeRow({
    actualMs: round2(actualMs),
    baseMs: round2(baseMs),
    commitTimeMs: round2(commitTime),
    id,
    phase,
    src: 'react',
    startTimeMs: round2(startTime),
    ts: Date.now()
  })
}

export function PerfPane({ children, id }: { children: ReactNode; id: string }) {
  if (!ENABLED) {
    return children
  }

  return (
    <Profiler id={id} onRender={onRender}>
      {children}
    </Profiler>
  )
}

export const logFrameEvent = ENABLED
  ? (event: FrameEvent) => {
      if (event.durationMs < THRESHOLD_MS) {
        return
      }

      writeRow({
        durationMs: round2(event.durationMs),

        fastPath: { ...scrollFastPathStats, declined: { ...scrollFastPathStats.declined } },
        flickers: event.flickers.length ? event.flickers : undefined,
        phases: event.phases
          ? {
              ...event.phases,
              commit: round2(event.phases.commit),
              diff: round2(event.phases.diff),
              optimize: round2(event.phases.optimize),
              prevFrameDrainMs: round2(event.phases.prevFrameDrainMs),
              renderer: round2(event.phases.renderer),
              write: round2(event.phases.write),
              yoga: round2(event.phases.yoga)
            }
          : undefined,
        src: 'frame',
        ts: Date.now()
      })
    }
  : undefined

export const PERF_ENABLED = ENABLED
export const PERF_LOG_PATH = LOG_PATH
