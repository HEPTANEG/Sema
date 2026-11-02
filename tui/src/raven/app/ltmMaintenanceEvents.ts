



import type { GatewayEvent } from '../gatewayTypes.js'
import type { Msg } from '../types.js'

import { compactPreview } from '../lib/text.js'
import { turnController } from './turnController.js'
import { patchUiState } from './uiStore.js'
import {
  appendLtmMaintenanceMessage,
  getLtmMaintenanceState,
  openLtmMaintenanceView,
  patchLtmMaintenanceState
} from './ltmMaintenanceStore.js'

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const asText = (value: unknown): string => (value == null ? '' : String(value))

const preview = (value: unknown, max = 160): string => {
  if (typeof value === 'string') {
    return compactPreview(value, max)
  }
  try {
    return compactPreview(JSON.stringify(value), max)
  } catch {
    return compactPreview(String(value), max)
  }
}

const firstErrorSummary = (result: Record<string, unknown>): string => {
  const errors = Array.isArray(result.errors) ? result.errors : []
  const first = errors[0]
  if (isRecord(first)) {
    const code = asText(first.code) || 'error'
    const message = asText(first.message) || preview(first)
    return `${code}: ${message}`
  }
  if (isRecord(result.error)) {
    const code = asText(result.error.code) || 'error'
    const message = asText(result.error.message) || preview(result.error)
    return `${code}: ${message}`
  }
  return preview(result)
}

const countValue = (result: Record<string, unknown>, countKey: string, idsKey: string): string => {
  const explicit = result[countKey]
  if (explicit !== undefined && explicit !== null) {
    return asText(explicit)
  }
  const ids = result[idsKey]
  return Array.isArray(ids) ? String(ids.length) : '0'
}

let finalCheckSequence = 0
let activeFinalCheckItemId: null | string = null

const resetFinalCheckTracking = () => {
  finalCheckSequence = 0
  activeFinalCheckItemId = null
}

const startFinalCheck = () => {
  const itemId = `ltm-maintenance-final-check-${finalCheckSequence++}`
  activeFinalCheckItemId = itemId
  turnController.recordToolStart(itemId, 'final_check', 'mechanical consistency')
}

const completeActiveFinalCheck = (error?: string, summary?: string): boolean => {
  if (!activeFinalCheckItemId) {
    return false
  }
  turnController.recordToolComplete(activeFinalCheckItemId, 'final_check', error, summary)
  activeFinalCheckItemId = null
  return true
}

export const isLtmMaintainCommand = (cmd: string): boolean => /^\/memory\s+ltm\s+maintain(?:\s|$)/i.test(cmd.trim())

export const formatLtmMaintenanceSummary = (result: Record<string, unknown> | null): string => {
  if (!result) {
    return 'LTM Maintenance finished.'
  }
  const state = isRecord(result.state) ? result.state : {}
  const lines = [
    `status: ${asText(result.status) || 'unknown'}`,
    `run_id: ${asText(result.run_id)}`,
    `coverage_cursor: ${asText(state.maintenance_coverage_cursor)}`,
    `final_check_status: ${asText(result.final_check_status)}`,
    `plan_commit_count: ${asText(result.plan_commit_count ?? 0)}`,
    `created_objects: ${asText(result.created_object_count ?? 0)}`,
    `created_links: ${asText(result.created_link_count ?? 0)}`,
    `deactivated_links: ${asText(result.deactivated_link_count ?? 0)}`,
    `updated_titles: ${asText(result.updated_title_count ?? 0)}`,
    `updated_summaries: ${asText(result.updated_summary_count ?? 0)}`
  ]
  if (result.failure_reason) {
    lines.push(`failure_reason: ${asText(result.failure_reason)}`)
  }
  return `LTM Maintenance:\n${lines.join('\n')}`
}

const ensureMaintenanceTurn = () => {
  const state = getLtmMaintenanceState()
  if (!state.active) {
    openLtmMaintenanceView('/memory ltm maintain')
  }
  patchUiState({ busy: true, status: 'maintaining LTM…' })
}

const finishMaintenanceTurn = (status: 'completed' | 'failed', result: Record<string, unknown>) => {
  const summary = formatLtmMaintenanceSummary(result)
  const { finalMessages } = turnController.recordMessageComplete({ text: summary })
  const messages: Msg[] = finalMessages.length ? finalMessages : [{ role: 'assistant', text: summary }]

  for (const msg of messages) {
    appendLtmMaintenanceMessage(msg)
  }

  patchLtmMaintenanceState(state => ({
    ...state,
    lastResult: result,
    runId: asText(result.run_id) || state.runId,
    status,
    summary,
    terminal: true
  }))
  activeFinalCheckItemId = null
  patchUiState({ busy: false, escapeArmed: false, status: status === 'completed' ? 'ready' : 'maintenance failed' })
}

export const settleLtmMaintenanceFromSlashResult = (result: Record<string, unknown> | null, fallbackOutput = '') => {
  const state = getLtmMaintenanceState()
  if (state.terminal) {
    return
  }

  const status = asText(result?.status).toLowerCase() === 'failed' ? 'failed' : 'completed'
  const payload = result && Object.keys(result).length ? result : { status, output: fallbackOutput }
  finishMaintenanceTurn(status, payload)
}

export const failLtmMaintenanceFromSlashError = (message: string) => {
  const state = getLtmMaintenanceState()
  if (!state.active || state.terminal) {
    return
  }
  finishMaintenanceTurn('failed', { failure_reason: message, status: 'failed' })
}

const toolSummary = (payload: Record<string, unknown>) => {
  const result = isRecord(payload.result) ? payload.result : {}
  const status = asText(payload.status || result.status || 'ok')
  if (payload.tool === 'retrieve_memory') {
    return `retrieval ${asText(result.kind) || status}: ${preview(result.message || result.error || '')}`
  }
  if (payload.type === 'ltm_maintenance_plan_committed' || payload.tool === 'submit_maintenance_plan') {
    if (status === 'rejected') {
      return `plan rejected: ${firstErrorSummary(result)}`
    }
    return `plan ${status}; objects ${countValue(result, 'created_object_count', 'object_ids')}, links ${countValue(result, 'created_link_count', 'link_ids')}`
  }
  if (payload.type === 'ltm_maintenance_coverage_advanced' || payload.tool === 'mark_maintenance_coverage') {
    return `coverage ${status}; cursor ${asText(result.maintenance_coverage_cursor ?? result.end_slot_seq ?? '')}`.trim()
  }
  return `${status}${Object.keys(result).length ? ` · ${preview(result)}` : ''}`
}

export const handleLtmMaintenanceEvent = (ev: GatewayEvent): boolean => {
  if (!ev.type.startsWith('ltm_maintenance_')) {
    return false
  }

  const payload = (ev.payload && isRecord(ev.payload) ? ev.payload : {}) as Record<string, unknown>
  const runId = asText(payload.run_id)

  switch (ev.type) {
  case 'ltm_maintenance_started':
      ensureMaintenanceTurn()
      resetFinalCheckTracking()
      turnController.startMessage()
      patchLtmMaintenanceState(state => ({
        ...state,
        runId: runId || state.runId,
        status: 'running',
        terminal: false
      }))
      return true

    case 'ltm_maintenance_stage_started': {
      ensureMaintenanceTurn()
      const stage = asText(payload.stage) || 'maintenance stage'
      const stageIndex = Number(payload.stage_index)
      patchLtmMaintenanceState(state => ({
        ...state,
        stage,
        stageIndex: Number.isFinite(stageIndex) ? stageIndex : state.stageIndex
      }))
      const itemId = `ltm-maintenance-stage-${asText(payload.stage_index) || stage}`
      turnController.recordToolStart(itemId, 'stage', stage)
      return true
    }

    case 'ltm_maintenance_stage_completed': {
      ensureMaintenanceTurn()
      const stage = asText(payload.stage) || 'maintenance stage'
      const itemId = `ltm-maintenance-stage-${asText(payload.stage_index) || stage}`
      turnController.recordToolComplete(itemId, 'stage', undefined, `${asText(payload.status) || 'completed'} · ${preview(payload.stage_summary ?? '', 120)}`)
      return true
    }

    case 'ltm_maintenance_model_turn':
      ensureMaintenanceTurn()
      turnController.recordEpisodeStart(Number(payload.plan_index ?? getLtmMaintenanceState().historyItems.length))
      return true

    case 'ltm_maintenance_model_error': {
      ensureMaintenanceTurn()
      const retry = asText(payload.retry)
      const maxRetries = asText(payload.max_retries)
      const itemId = `ltm-maintenance-model-error-${retry || Date.now()}`
      const label = maxRetries ? `retry ${retry}/${maxRetries}` : 'retry'
      turnController.recordToolStart(itemId, 'model_retry', label)
      turnController.recordToolComplete(itemId, 'model_retry', preview(payload.error ?? payload), preview(payload.diagnostics ?? {}))
      return true
    }

    case 'ltm_maintenance_reasoning_available': {
      ensureMaintenanceTurn()
      const content = asText(payload.content)
      if (content.trim()) {
        turnController.recordReasoningDelta(content)
      }
      return true
    }

    case 'ltm_maintenance_message_delta': {
      ensureMaintenanceTurn()
      const content = asText(payload.content)
      if (content.trim()) {
        turnController.recordMessageDelta({ text: content })
      }
      return true
    }

    case 'ltm_maintenance_runtime_notice':
    case 'ltm_maintenance_repetition_warning': {
      ensureMaintenanceTurn()
      const content = asText(payload.content)
      if (content.trim()) {
        turnController.recordMessageDelta({ text: `\n\nRuntime warning: ${content}\n` })
        turnController.flushStreamingSegment()
      }
      return true
    }

    case 'ltm_maintenance_tool_started':
      ensureMaintenanceTurn()
      turnController.recordToolStart(
        asText(payload.item_id) || `${asText(payload.tool) || 'maintenance'}-${Date.now()}`,
        asText(payload.tool) || 'maintenance_tool',
        preview(payload.args ?? '')
      )
      return true

    case 'ltm_maintenance_tool_finished':
    case 'ltm_maintenance_plan_committed':
    case 'ltm_maintenance_coverage_advanced':
      ensureMaintenanceTurn()
      turnController.recordToolComplete(
        asText(payload.item_id) || `${asText(payload.tool) || 'maintenance'}-${Date.now()}`,
        asText(payload.tool) || 'maintenance_tool',
        asText(payload.status).toLowerCase() === 'error' || asText(payload.status).toLowerCase() === 'failed'
          ? payload.tool === 'retrieve_memory' ? toolSummary(payload) : preview(payload.result ?? payload.status)
          : undefined,
        toolSummary({ ...payload, type: ev.type })
      )
      return true

    case 'ltm_maintenance_final_check_started':
      ensureMaintenanceTurn()
      startFinalCheck()
      return true

    case 'ltm_maintenance_final_check_failed':
      ensureMaintenanceTurn()
      completeActiveFinalCheck('final check failed', preview(payload.health ?? payload))
      return true

    case 'ltm_maintenance_completed':
      ensureMaintenanceTurn()
      completeActiveFinalCheck(undefined, 'final check passed')
      finishMaintenanceTurn('completed', payload)
      return true

    case 'ltm_maintenance_failed':
      ensureMaintenanceTurn()
      completeActiveFinalCheck('maintenance failed', preview(payload))
      finishMaintenanceTurn('failed', payload)
      return true

    default:
      return true
  }
}
