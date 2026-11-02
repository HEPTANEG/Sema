


import type { BridgeEvent, ContextPressureTelemetry, EditStats, InitializeResult } from './protocol/types.js'
import type { SessionInfo } from './raven/types.js'

export const errorMessage = (error: unknown): string => {
  if (error instanceof Error && error.message) {
    return error.message
  }

  if (error && typeof error === 'object' && 'message' in error) {
    const message = (error as {message?: unknown}).message
    if (typeof message === 'string' && message) {
      return message
    }
  }

  return String(error)
}

export const unknownSlashCommandMessage = (command: string, result: Record<string, unknown>): string | null => {
  if (result.handled !== false) {
    return null
  }

  return `Unknown slash command: ${command}. Use /help to list available commands.`
}

export const backgroundTaskEvent = (type: string): { key: string; phase: 'finished' | 'started' } | null => {
  if (type.endsWith('_started')) {
    return {key: type, phase: 'started'}
  }

  if (type.endsWith('_finished')) {
    return {key: type.replace(/_finished$/, '_started'), phase: 'finished'}
  }

  return null
}

export const isCurrentTurnEvent = (eventTurnId: string, activeTurnId: string | null): boolean => (
  Boolean(eventTurnId && activeTurnId && eventTurnId === activeTurnId)
)

export const backgroundTaskLabel = (type: string) => {
  const labels: Record<string, string> = {
    focus_detector_started: 'Focus Detector is working',
    full_segment_maintenance_started: 'Focus Maintainer is running',
    segment_ingest_started: 'Focus Ingester is running',
    working_memory_maintenance_started: 'Working Memory Maintainer is working',
    working_memory_preflight_started: 'WM Preflight Recovery is working',
    retriever_started: 'LTM Retriever is running',
    memory_push_started: 'LTM Push is running',
    ltm_formation_started: 'LTM Formationer is running',
    formation_backfill_started: 'LTM Formationer (Backfill) is running',
    memorize_started: 'LTM Memorizer is running',
    ltm_maintenance_started: 'LTM Maintainer is running'
  }

  return labels[type] ?? type.replace(/_started$/, '').replaceAll('_', ' ')
}

export const eventLabel = (event: BridgeEvent<Record<string, unknown>>) => {
  const label = event.payload.label
  return typeof label === 'string' && label.trim() ? label : event.type.replaceAll('_', ' ')
}

export const toolId = (event: BridgeEvent<Record<string, unknown>>) => String(event.payload.item_id || event.event_id)

export const toolSummary = (payload: Record<string, unknown>) => {
  const args = payload.args
  if (typeof args === 'string') return args.slice(0, 180)
  if (args && typeof args === 'object') {
    try {
      return JSON.stringify(args).slice(0, 180)
    } catch {
      return ''
    }
  }
  return ''
}

export const normalizeContextPressure = (payload: Record<string, unknown>): ContextPressureTelemetry => ({
  budget_source: typeof payload.budget_source === 'string' ? payload.budget_source : undefined,
  stm: optionalNumber(payload.stm),
  stm_min: optionalNumber(payload.stm_min),
  stm_max: optionalNumber(payload.stm_max),
  wm: optionalNumber(payload.wm),
  wm_min: optionalNumber(payload.wm_min),
  wm_max: optionalNumber(payload.wm_max),
  I: optionalNumber(payload.I),
  S: optionalNumber(payload.S),
  R: optionalNumber(payload.R),
  L: optionalNumber(payload.L),
  P_context: optionalNumber(payload.P_context)
})

export const normalizeEditStats = (value: unknown): EditStats | null => {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const added = Number(record.added ?? 0)
  const removed = Number(record.removed ?? 0)
  if (!Number.isFinite(added) || !Number.isFinite(removed) || (added === 0 && removed === 0)) return null
  const files = Array.isArray(record.files) ? record.files.map(file => String(file)).filter(Boolean) : []
  return {added, files, removed, tool: String(record.tool ?? '')}
}

export const addEditStats = (current: EditStats, next: EditStats): EditStats => ({
  added: current.added + next.added,
  files: Array.from(new Set([...(current.files || []), ...(next.files || [])])),
  removed: current.removed + next.removed
})

export const formatSemaPersistentStatus = ({
  changes,
  contextPressure,
  responseTimeMs,
  activeFocusTitle
}: {
  changes: EditStats
  contextPressure: ContextPressureTelemetry | null
  responseTimeMs: number | null
  activeFocusTitle?: string | null
}) => {
  const parts = [formatBudgetPair(contextPressure)]

  if (activeFocusTitle?.trim()) {
    parts.push(`Focus: ${activeFocusTitle.trim()}`)
  }

  if (changes.added || changes.removed) {
    parts.push(`Changes +${changes.added} -${changes.removed}`)
  }

  if (responseTimeMs !== null) {
    parts.push(`Last response ${formatDuration(responseTimeMs)}`)
  }

  return parts.join(' | ')
}

export const formatSemaProcessStatus = (foreground: string, backgroundLabels: Iterable<string>, notice?: string | null) => {
  if (notice?.trim()) return notice.trim()

  const parts = [foreground.trim().replace(/\.+$/, ''), ...Array.from(backgroundLabels).map(label => label.trim())]
    .filter(Boolean)

  return parts.length ? parts.join(' | ') : 'Nothing is happening'
}

const formatBudgetPair = (context: ContextPressureTelemetry | null) =>
  `STM ${formatBudget(context?.stm, context?.stm_max)} | WM ${formatBudget(context?.wm, context?.wm_max)}`

const formatBudget = (value?: number, maximum?: number) => {
  if (value === undefined && maximum === undefined) return 'n/a'
  if (maximum === undefined || maximum <= 0) return formatTokenCount(value)
  return `${formatTokenCount(value)}/${formatTokenCount(maximum)}`
}

const formatTokenCount = (value?: number) => {
  if (value === undefined || !Number.isFinite(value)) return 'n/a'
  const rounded = Math.max(0, Math.round(value))
  if (rounded >= 1000) {
    const compact = rounded / 1000
    return `${compact >= 10 ? compact.toFixed(0) : compact.toFixed(1)}k`
  }
  return String(rounded)
}

const formatDuration = (durationMs: number) => {
  const seconds = Math.max(0, durationMs) / 1000
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.round(seconds % 60)
  return `${minutes}m ${String(remainder).padStart(2, '0')}s`
}

const optionalNumber = (value: unknown) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : undefined
}

export const makeSessionInfo = (result: InitializeResult): SessionInfo => {
  const project = result.project ?? {}
  const model = result.model || 'unknown'
  const provider = model.includes(':') ? model.split(':', 1)[0] : undefined

  return {
    cwd: String(project.root ?? project.cwd ?? process.cwd()),
    model,
    model_id: model,
    provider,
    tools: {},
    version: undefined
  }
}
