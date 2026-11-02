import {describe, expect, it} from 'vitest'

import type { InitializeResult } from './protocol/types.js'
import {
  backgroundTaskEvent,
  backgroundTaskLabel,
  errorMessage,
  eventLabel,
  formatSemaPersistentStatus,
  formatSemaProcessStatus,
  isCurrentTurnEvent,
  makeSessionInfo,
  normalizeContextPressure,
  toolId,
  toolSummary,
  unknownSlashCommandMessage
} from './semaRavenAdapter.js'

const init = (project: Record<string, unknown> = {}): InitializeResult => ({
  continued_session: true,
  model: 'deepseek:deepseek-chat',
  permissions: {},
  project,
  slash_commands: []
})

describe('Sema Raven adapter', () => {
  it('accepts focus events only for the active turn', () => {
    expect(isCurrentTurnEvent('turn-1', 'turn-1')).toBe(true)
    expect(isCurrentTurnEvent('turn-1', 'turn-2')).toBe(false)
    expect(isCurrentTurnEvent('', 'turn-1')).toBe(false)
    expect(isCurrentTurnEvent('turn-1', null)).toBe(false)
  })

  it('normalizes slash command errors into visible messages', () => {
    expect(errorMessage(new Error('no such column: run_start_cursor'))).toBe('no such column: run_start_cursor')
    expect(errorMessage({message: 'backend failed'})).toBe('backend failed')
    expect(unknownSlashCommandMessage('/try', {handled: false})).toContain('Unknown slash command: /try')
    expect(unknownSlashCommandMessage('/help', {handled: true})).toBeNull()
  })

  it('maps Sema session metadata to Raven session info', () => {
    expect(makeSessionInfo(init({root: 'C:\\workspace'}))).toMatchObject({
      cwd: 'C:\\workspace',
      model: 'deepseek:deepseek-chat',
      provider: 'deepseek'
    })
  })

  it('maps background lifecycle events without leaking finish names', () => {
    expect(backgroundTaskEvent('focus_detector_started')).toEqual({key: 'focus_detector_started', phase: 'started'})
    expect(backgroundTaskEvent('focus_detector_finished')).toEqual({key: 'focus_detector_started', phase: 'finished'})
    expect(backgroundTaskLabel('retriever_started')).toBe('LTM Retriever is running')
    expect(backgroundTaskLabel('segment_ingest_started')).toBe('Focus Ingester is running')
    expect(backgroundTaskLabel('full_segment_maintenance_started')).toBe('Focus Maintainer is running')
    expect(backgroundTaskLabel('memory_push_started')).toBe('LTM Push is running')
    expect(backgroundTaskLabel('ltm_formation_started')).toBe('LTM Formationer is running')
    expect(backgroundTaskLabel('formation_backfill_started')).toBe('LTM Formationer (Backfill) is running')
    expect(backgroundTaskLabel('memorize_started')).toBe('LTM Memorizer is running')
    expect(backgroundTaskLabel('ltm_maintenance_started')).toBe('LTM Maintainer is running')
    expect(backgroundTaskEvent('retriever_finished')).toEqual({key: 'retriever_started', phase: 'finished'})
    expect(backgroundTaskEvent('context_pressure')).toBeNull()
  })

  it('uses stable tool ids and bounded argument summaries', () => {
    const event = {event_id: 'event-1', payload: {item_id: 'tool-1'}, turn_id: 'turn-1', type: 'tool_started'}
    expect(toolId(event)).toBe('tool-1')
    expect(toolSummary({args: {path: 'README.md'}})).toBe('{"path":"README.md"}')
  })

  it('prefers the backend label for progress status', () => {
    expect(eventLabel({event_id: 'e', payload: {label: 'Planning task'}, turn_id: 't', type: 'assistant_stage'})).toBe(
      'Planning task'
    )
    expect(eventLabel({event_id: 'e', payload: {}, turn_id: 't', type: 'reviewer_started'})).toBe('reviewer started')
  })

  it('formats Sema persistent status without Raven model, context gauge, or cwd fields', () => {
    const contextPressure = normalizeContextPressure({stm: 13200, stm_max: 50000, wm: 9100, wm_max: 50000})
    expect(formatSemaPersistentStatus({
      changes: {added: 105, files: ['a.ts'], removed: 90},
      contextPressure,
      responseTimeMs: 2250
    })).toBe('STM 13k/50k | WM 9.1k/50k | Changes +105 -90 | Last response 2s')
    expect(formatSemaPersistentStatus({
      changes: {added: 0, files: [], removed: 0},
      contextPressure: null,
      responseTimeMs: 8100
    })).toContain('Last response 8s')
  })

  it('keeps a stable process row with an idle placeholder', () => {
    expect(formatSemaProcessStatus('', [])).toBe('Nothing is happening')
    expect(formatSemaProcessStatus('Selecting long-term memory...', [
      backgroundTaskLabel('focus_detector_started')
    ])).toBe('Selecting long-term memory | Focus Detector is working')
    expect(formatSemaProcessStatus('', [backgroundTaskLabel('retriever_started')])).toBe('LTM Retriever is running')
    expect(formatSemaProcessStatus('Main Agent is running', ['Focus Detector is working'])).toBe('Main Agent is running | Focus Detector is working')
    expect(formatSemaProcessStatus('running', ['Focus Detector is working'], 'Focus shifted: New topic')).toBe('Focus shifted: New topic')
    expect(formatSemaPersistentStatus({changes: {added: 0, files: [], removed: 0}, contextPressure: null, responseTimeMs: null, activeFocusTitle: 'New topic'})).toContain('Focus: New topic')
  })
})
