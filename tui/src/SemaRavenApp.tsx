



import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useApp, useHasSelection, useInput, useSelection, useStdout, type ScrollBoxHandle } from '@hermes/ink'

import { AppLayout } from './raven/components/appLayout.js'
import { CommandOutputPanel } from './components/CommandOutputPanel.js'
import { ConfigPanel } from './components/ConfigPanel.js'
import { SettingsPanel } from './components/SettingsPanel.js'
import {SetupPanel} from './components/SetupPanel.js'
import {$setup, $setupPage, $configReturnView} from './stores/runtime_state.js'
import { GatewayProvider } from './raven/app/gatewayContext.js'
import { DEFAULT_VOICE_RECORD_KEY } from './raven/lib/platform.js'
import { DEFAULT_THEME } from './raven/theme.js'
import type {
  AppLayoutActions,
  AppLayoutComposerProps,
  CompletionItem,
  AppLayoutProgressProps,
  AppLayoutStatusProps,
  AppLayoutTranscriptProps
} from './raven/app/interfaces.js'
import type { Msg } from './raven/types.js'
import { $overlayState, resetOverlayState } from './raven/app/overlayStore.js'
import { $uiState, patchUiState } from './raven/app/uiStore.js'
import { turnController } from './raven/app/turnController.js'
import { patchTurnState } from './raven/app/turnStore.js'
import {
  closeLtmMaintenanceView,
  getLtmMaintenanceState,
  openLtmMaintenanceView,
  $ltmMaintenanceState
} from './raven/app/ltmMaintenanceStore.js'
import {
  failLtmMaintenanceFromSlashError,
  failLtmMaintenanceFromBackendDisconnect,
  handleLtmMaintenanceEvent,
  isLtmMaintainCommand,
  settleLtmMaintenanceFromSlashResult
} from './raven/app/ltmMaintenanceEvents.js'
import { BridgeClient } from './protocol/client.js'
import type { BridgeEvent, ConfirmationRequest, ContextPressureTelemetry, EditStats, InitializeResult, SlashCommand } from './protocol/types.js'
import type { GatewayEvent } from './raven/gatewayTypes.js'
import { matchingSlashCommands, resolveSemaSlashInput } from './commands/slash.js'
export { resolveSemaSlashInput } from './commands/slash.js'
import { getInputSelection } from './raven/app/inputSelectionStore.js'
import { writeClipboardText } from './raven/lib/clipboard.js'
import {
  addEditStats,
  backgroundTaskEvent,
  backgroundTaskLabel,
  errorMessage,
  eventLabel,
  formatSemaPersistentStatus,
  formatSemaProcessStatus,
  makeSessionInfo,
  normalizeContextPressure,
  normalizeEditStats,
  isCurrentTurnEvent,
  toolId,
  toolSummary,
  unknownSlashCommandMessage
} from './semaRavenAdapter.js'
import {
  $activeView,
  $commandOutput,
  $config,
  $panelScroll,
  $pendingConfirmation,
  $settings,
  $slashCommands,
  $terminalColumns,
  $terminalRows
} from './stores/runtime_state.js'
import { MOUSE_TRACKING } from './raven/config/env.js'
import {applyNotificationSettings, enableTerminalFocusTracking, maybeNotifyBridgeEvent} from './notifications.js'

type HistoryRow = { key: string; msg: Msg }

type SemaCtrlCAction = 'copy-input-selection' | 'copy-terminal-selection' | 'noop'

export function decideSemaCtrlC({
  hasInputSelection,
  hasTerminalSelection
}: {
  hasInputSelection: boolean
  hasTerminalSelection: boolean
}): SemaCtrlCAction {
  if (hasTerminalSelection) return 'copy-terminal-selection'
  if (hasInputSelection) return 'copy-input-selection'
  return 'noop'
}

function openCommandOutput(title: string, content: string) {
  $commandOutput.set({title, content})
  $panelScroll.set(0)
  $activeView.set('commandOutput')
}

export function semaSlashCompletions(commands: SlashCommand[], input: string, limit = 12): CompletionItem[] {
  return matchingSlashCommands(commands, input, limit).slice().reverse().map(command => ({
    display: command.name,
    meta: command.help,
    text: command.name
  }))
}

function closeCommandOutput() {
  if ($activeView.get() !== 'commandOutput') return
  $commandOutput.set(null)
  $panelScroll.set(0)
  $activeView.set('chat')
}

export function applySemaSlashResult(command: string, row: Record<string, unknown>) {
  applyNotificationSettings(row.notifications)
  if (row.settings && typeof row.settings === 'object') {
    $settings.set(row.settings as Record<string, unknown>)
    $activeView.set('settings')
    return {shouldExit: false}
  }
  if (row.config && typeof row.config === 'object') {
    $configReturnView.set('chat')
    $config.set(row.config as Record<string, unknown>)
    $activeView.set('config')
    return {shouldExit: false}
  }
  const unknownCommand = unknownSlashCommandMessage(command, row)
  if (unknownCommand) {
    openCommandOutput('Command output', unknownCommand)
    patchUiState({busy: false, status: 'error'})
    return {shouldExit: false}
  }
  const content = String(row.content ?? '')
  if (content) openCommandOutput(String(row.title || 'Command output'), content)
  return {shouldExit: Boolean(row.should_exit || row.shouldExit)}
}

function useRavenTranscript(
  client: BridgeClient,
  initialized: InitializeResult | null,
  onTurnStarted: () => void,
  onTurnFinished: () => void,
  onContextPressure: (context: ContextPressureTelemetry) => void,
  onEditStats: (stats: EditStats) => void
) {
  const [rows, setRows] = useState<HistoryRow[]>([])
  const rowsRef = useRef(rows)
  const seenEvents = useRef(new Set<string>())
  const nextKey = useRef(0)
  const nextEpisode = useRef(0)
  const episodeNeedsStart = useRef(false)
  const turnActive = useRef(false)
  const activeTurnId = useRef<string | null>(null)
  const focusShiftTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  rowsRef.current = rows

  const append = useCallback((msg: Msg) => {
    if (!msg.text && msg.kind !== 'episodes' && msg.kind !== 'trail') return
    setRows(current => [...current, { key: `msg:${++nextKey.current}`, msg }])
  }, [])

  useEffect(() => {
    if (!initialized) return

    const intro: Msg = { kind: 'intro', role: 'system', text: '', info: makeSessionInfo(initialized) }
    setRows([{ key: 'intro', msg: intro }])
    activeTurnId.current = null
    if (focusShiftTimer.current) {
      clearTimeout(focusShiftTimer.current)
      focusShiftTimer.current = null
    }
    turnController.fullReset()
    resetOverlayState()
    patchUiState({
      bgTasks: new Set(),
      busy: false,
      focusShiftNotice: null,
      detailsMode: 'collapsed',
      info: makeSessionInfo(initialized),
      mouseTracking: MOUSE_TRACKING,
      sid: String(initialized.project?.active_conversation_id ?? 'event-stream'),
      status: 'ready',
      statusBar: 'bottom',
      streaming: true,
      theme: DEFAULT_THEME,
      usage: {calls: 0, input: 0, output: 0, total: 0}
    })
    $slashCommands.set(initialized.slash_commands ?? [])
  }, [initialized])

  useEffect(() => {
    const onEvent = (event: BridgeEvent<Record<string, unknown>>) => {
      if (event.event_id && seenEvents.current.has(event.event_id)) return
      if (event.event_id) seenEvents.current.add(event.event_id)
      maybeNotifyBridgeEvent(event)



      if (handleLtmMaintenanceEvent(event as unknown as GatewayEvent)) {
        return
      }

      const payload = event.payload ?? {}

      switch (event.type) {
        case 'user_message': {
          const content = String(payload.content ?? '')
          const last = rowsRef.current.at(-1)?.msg
          if (last?.role !== 'user' || last.text !== content) append({role: 'user', text: content})
          break
        }
        case 'turn_started':
          activeTurnId.current = event.turn_id || null
          if (focusShiftTimer.current) {
            clearTimeout(focusShiftTimer.current)
            focusShiftTimer.current = null
          }
          turnActive.current = true
          onTurnStarted()
          episodeNeedsStart.current = false
          nextEpisode.current = 0
          turnController.startMessage()
          turnController.recordEpisodeStart(0)


          patchUiState({busy: true, status: '', focusShiftNotice: null})
          break
        case 'wake_delivered':
          if (
            payload.source === 'maintainer'
            && (
              payload.kind === 'maintenance_completed'
              || payload.kind === 'maintenance_task_resumed'
              || payload.kind === 'maintenance_sleep_failed'
            )
          ) {
            closeLtmMaintenanceView()
            patchUiState({
              busy: true,
              status: payload.kind === 'maintenance_task_resumed'
                ? 'resuming task after LTM maintenance…'
                : payload.kind === 'maintenance_sleep_failed'
                  ? 'handling LTM maintenance failure…'
                  : 'waking from LTM maintenance…'
            })
          }
          break
        case 'context_pressure':
          onContextPressure(normalizeContextPressure(payload))
          break
        case 'intent_routing_started':
        case 'assistant_stage':
        case 'coordinator_state_updating':
        case 'reviewer_started':
          patchUiState({status: eventLabel(event)})
          turnController.pushActivity(eventLabel(event), 'info')
          break
        case 'model_thinking':


          patchUiState({busy: true, status: ''})
          break
        case 'focus_detector_started':
        case 'working_memory_preflight_started':
        case 'working_memory_maintenance_started':
        case 'segment_ingest_started':
        case 'full_segment_maintenance_started':
        case 'retriever_started':
        case 'memory_push_started':
        case 'ltm_formation_started':
        case 'formation_backfill_started':
        case 'memorize_started':
        case 'ltm_maintenance_started':
          patchUiState(state => {
            if (event.type === 'focus_detector_started' && !isCurrentTurnEvent(event.turn_id, activeTurnId.current)) {
              return state
            }
            const bgTasks = new Set(state.bgTasks)
            bgTasks.add(backgroundTaskLabel(event.type))
            return event.type === 'focus_detector_started'
              ? {...state, bgTasks, focusShiftNotice: null}
              : {...state, bgTasks}
          })
          break
        case 'focus_shifted': {
          if (!isCurrentTurnEvent(event.turn_id, activeTurnId.current)) break
          const title = String(payload.title ?? '').trim()
          if (!title) break
          if (focusShiftTimer.current) clearTimeout(focusShiftTimer.current)
          patchUiState(state => {
            const bgTasks = new Set(state.bgTasks)
            bgTasks.delete(backgroundTaskLabel('focus_detector_started'))
            return {...state, bgTasks, activeFocusTitle: title, focusShiftNotice: `Focus shifted: ${title}`}
          })
          focusShiftTimer.current = setTimeout(() => {
            patchUiState({focusShiftNotice: null})
            focusShiftTimer.current = null
          }, 4000)
          break
        }
        case 'focus_detector_finished':
          if (!isCurrentTurnEvent(event.turn_id, activeTurnId.current)) break
          patchUiState(state => {
            const bgTasks = new Set(state.bgTasks)
            bgTasks.delete(backgroundTaskLabel('focus_detector_started'))
            return {...state, bgTasks, activeFocusTitle: String(payload.active_title ?? '').trim() || null}
          })
          break
        case 'working_memory_preflight_finished':
        case 'working_memory_maintenance_finished':
        case 'segment_ingest_finished':
        case 'full_segment_maintenance_finished':
        case 'retriever_finished':
        case 'memory_push_finished':
        case 'ltm_formation_finished':
        case 'formation_backfill_finished':
        case 'memorize_finished':
        case 'ltm_maintenance_completed':
        case 'ltm_maintenance_failed':
          patchUiState(state => {
            const bgTasks = new Set(state.bgTasks)
            const activity = backgroundTaskEvent(event.type)
            const key = activity?.key ?? (event.type.startsWith('ltm_maintenance_') ? 'ltm_maintenance_started' : null)
            if (key) bgTasks.delete(backgroundTaskLabel(key))
            return {...state, bgTasks}
          })
          break
        case 'assistant_delta': {
          if (episodeNeedsStart.current) {
            episodeNeedsStart.current = false
            nextEpisode.current += 1
            turnController.recordEpisodeStart(nextEpisode.current)
          }
          const delta = String(payload.delta ?? '')
          if (delta) {
            turnController.recordMessageDelta({text: delta})
            patchUiState({busy: true, status: 'Main Agent is running'})
          }
          break
        }
        case 'assistant_content':



          {
            const content = String(payload.content ?? '')
            if (content) {
              turnController.recordMessageDelta({text: content})
              patchUiState({busy: true, status: 'Main Agent is running'})
            }
          }
          break
        case 'tool_started': {
          const id = toolId(event)
          turnController.recordToolStart(id, String(payload.tool ?? 'tool'), toolSummary(payload))
          episodeNeedsStart.current = true
          patchUiState({busy: true, status: `running ${String(payload.tool ?? 'tool')}`})
          break
        }
        case 'tool_finished': {
          const status = String(payload.status ?? 'ok')
          const error = status === 'error' ? String(payload.error ?? payload.result ?? 'tool failed') : undefined
          const result = String(payload.result ?? '')
          const duration = Number(payload.duration_ms)
          turnController.recordToolComplete(
            toolId(event),
            String(payload.tool ?? 'tool'),
            error,
            result,
            Number.isFinite(duration) ? duration / 1000 : undefined
          )
          const editStats = normalizeEditStats(payload.edit_stats)
          if (editStats) onEditStats(editStats)
          patchUiState({status: ''})
          break
        }
        case 'final_answer': {
          const content = String(payload.content ?? '')
          const result = turnController.recordMessageComplete({text: content})
          for (const message of result.finalMessages) append(message)
          turnActive.current = false
          onTurnFinished()
          episodeNeedsStart.current = false
          $pendingConfirmation.set(null)
          $activeView.set('chat')
          patchUiState({busy: false, status: 'ready'})
          break
        }
        case 'confirmation_requested': {
          const request = payload as unknown as ConfirmationRequest
          $pendingConfirmation.set(request)
          $activeView.set('confirm')
          patchTurnState({outcome: `confirmation required: ${request.title ?? 'action'}`})
          patchUiState({busy: true, status: 'waiting for confirmation'})
          break
        }
        case 'turn_cancelled':
        case 'agent_stopped':
          append({role: 'assistant', text: String(payload.message ?? 'Turn cancelled by user.')})
          turnController.reset()
          turnActive.current = false
          onTurnFinished()
          $pendingConfirmation.set(null)
          $activeView.set('chat')
          patchUiState({busy: false, status: 'ready'})
          break
        case 'agent_error':
        case 'error':
        case 'model_error':
          append({role: 'assistant', text: String(payload.error ?? payload.message ?? 'Backend error')})
          turnController.recordError()
          turnActive.current = false
          onTurnFinished()
          $pendingConfirmation.set(null)
          $activeView.set('chat')
          patchUiState({busy: false, status: 'error'})
          break
        default:
          if (event.type.endsWith('_finished') || event.type === 'reviewer_finished') {
            if (!turnActive.current) patchUiState({status: 'ready'})
          }
      }
    }

    client.on('event', onEvent)
    return () => {
      client.off('event', onEvent)
      if (focusShiftTimer.current) clearTimeout(focusShiftTimer.current)
    }
  }, [append, client, onContextPressure, onEditStats, onTurnFinished, onTurnStarted])

  const virtualRows = useMemo(
    () => rows.map((row, index) => ({index, key: row.key, msg: row.msg})),
    [rows]
  )

  return {append, rows, virtualRows}
}

export function SemaRavenApp() {
  const {exit} = useApp()
  const {stdout} = useStdout()
  const client = useMemo(() => new BridgeClient(), [])
  const [initialized, setInitialized] = useState<InitializeResult | null>(null)
  const [input, setInput] = useState('')
  const [cols, setCols] = useState(stdout?.columns ?? 80)
  const [sessionStartedAt, setSessionStartedAt] = useState<number | null>(null)
  const [turnStartedAt, setTurnStartedAt] = useState<number | null>(null)
  const [lastTurnDurationMs, setLastTurnDurationMs] = useState<number | null>(null)
  const [contextPressure, setContextPressure] = useState<ContextPressureTelemetry | null>(null)
  const [turnCodeChanges, setTurnCodeChanges] = useState<EditStats>({added: 0, files: [], removed: 0})
  const [now, setNow] = useState(Date.now())
  const scrollRef = useRef<ScrollBoxHandle | null>(null)
  const turnStartedAtRef = useRef<number | null>(null)
  const onTurnStarted = useCallback(() => {
    const startedAt = Date.now()
    turnStartedAtRef.current = startedAt
    setTurnStartedAt(startedAt)
    setTurnCodeChanges({added: 0, files: [], removed: 0})
  }, [])
  const onTurnFinished = useCallback(() => {
    const startedAt = turnStartedAtRef.current
    if (startedAt !== null) {
      setLastTurnDurationMs(Math.max(0, Date.now() - startedAt))
    }
    turnStartedAtRef.current = null
    setTurnStartedAt(null)
  }, [])
  const onEditStats = useCallback((stats: EditStats) => {
    setTurnCodeChanges(current => addEditStats(current, stats))
  }, [])
  const {append, rows, virtualRows} = useRavenTranscript(
    client,
    initialized,
    onTurnStarted,
    onTurnFinished,
    setContextPressure,
    onEditStats
  )
  const slashCommands = useStore($slashCommands)
  const maint = useStore($ltmMaintenanceState)
  const activeView = useStore($activeView)
  const setup = useStore($setup)
  const config = useStore($config)
  const settings = useStore($settings)
  const commandOutput = useStore($commandOutput)
  const ui = useStore($uiState)
  const overlay = useStore($overlayState)
  const selection = useSelection()
  const hasSelection = useHasSelection()

  useEffect(() => {
    const resize = () => {
      const columns = Math.max(20, stdout?.columns ?? 80)
      const rows = Math.max(12, stdout?.rows ?? 24)
      setCols(columns)
      $terminalColumns.set(columns)
      $terminalRows.set(rows)
    }
    stdout?.on('resize', resize)
    const onReinitialized = (result: InitializeResult) => {
      setInitialized(result)
      if ((result as any).setup) $setup.set((result as any).setup)
      applyNotificationSettings(result.notifications || {enabled: true})
      void client.executeSlash('/settings').then(value => {$settings.set((value as any).settings)}).catch(() => {})
    }
    const onBackendExit = () => failLtmMaintenanceFromBackendDisconnect()
    client.on('reinitialized', onReinitialized)
    client.on('exit', onBackendExit)
    const disableFocusTracking = enableTerminalFocusTracking()
    void client.initialize().then(result => {
      applyNotificationSettings(result.notifications || {enabled: true})
      setSessionStartedAt(Date.now())
      setContextPressure(result.context_pressure ? normalizeContextPressure(result.context_pressure as unknown as Record<string, unknown>) : null)
      setInitialized(result)
      const preparation = (result as any).setup
      if (preparation) {
        $setup.set(preparation)
        if (!preparation.ltm_enabled || preparation.components.llm.status !== 'Ready') {
          $setupPage.set('overview')
          $settings.set({categories: []})
          client.executeSlash('/settings').then(result => {const row = result as any; $settings.set(row.settings)}).catch(() => {})
          $activeView.set('setup')
        }
      }
    }).catch(error => {
      append({role: 'assistant', text: String(error instanceof Error ? error.message : error)})
      patchUiState({status: 'error'})
    })
    return () => {
      stdout?.off('resize', resize)
      client.off('reinitialized', onReinitialized)
      client.off('exit', onBackendExit)
      disableFocusTracking()
      client.dispose()
    }
  }, [append, client, stdout])

  useEffect(() => {
    if (turnStartedAt === null) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [turnStartedAt])

  useEffect(() => {
    selection.setSelectionBgColor(ui.theme.color.selectionBg)
  }, [selection, ui.theme.color.selectionBg])

  useInput((value, key) => {
    const maintenance = getLtmMaintenanceState()
    if (maintenance.active) {
      if (maintenance.terminal && (key.return || key.escape)) {
        closeLtmMaintenanceView()
        patchUiState({busy: false, escapeArmed: false, status: 'ready'})
        return
      }
      if (maintenance.status === 'running') {
        return
      }
    }

    if (key.ctrl && value.toLowerCase() === 'c') {
      const inputSelection = getInputSelection()
      switch (
        decideSemaCtrlC({
          hasInputSelection: Boolean(inputSelection && inputSelection.end > inputSelection.start),
          hasTerminalSelection: hasSelection
        })
      ) {
        case 'copy-terminal-selection':
          void selection.copySelectionNoClear()
          return
        case 'copy-input-selection':
          void writeClipboardText(inputSelection!.value.slice(inputSelection!.start, inputSelection!.end))
          return
        case 'noop':
          return
      }
    }

    if (key.ctrl && value.toLowerCase() === 'x' && ui.busy && !overlay.confirm) {
      void client.cancelTurn().catch(() => {})
    }
  }, {isActive: true})

  const submit = useCallback((value: string) => {
    const text = value.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    if (!text.trim() || ui.busy || overlay.confirm) return
    setInput('')
    const trimmed = text.trim()
    const commandText = trimmed.startsWith('/') ? resolveSemaSlashInput(slashCommands, trimmed) : trimmed
    closeCommandOutput()

    if (commandText.startsWith('/')) {
      append({role: 'user', text: commandText})

      if (isLtmMaintainCommand(commandText)) {
        openLtmMaintenanceView(commandText)
        patchUiState({busy: true, escapeArmed: false, status: 'maintaining LTM…'})
      }

      void client.executeSlash(commandText).then(result => {
        const row = result as Record<string, unknown>
        if (isLtmMaintainCommand(commandText)) {
          settleLtmMaintenanceFromSlashResult(row, String(row.content ?? ''))
          return
        }
        if (applySemaSlashResult(commandText, row).shouldExit) return exit()
      }).catch(error => {
        if (isLtmMaintainCommand(commandText)) {
          failLtmMaintenanceFromSlashError(errorMessage(error))
          return
        }
        openCommandOutput('Command failed', `Slash command failed: ${errorMessage(error)}`)
        patchUiState({busy: false, status: 'error'})
      })
      return
    }

    void client.sendMessage(text).then(result => {
      const response = result as Record<string, unknown> | null
      if (response?.maintenance_started) {



        setInput(text)
      }
    }).catch(error => {
      append({role: 'assistant', text: String(error instanceof Error ? error.message : error)})
      patchUiState({busy: false, status: 'error'})
    })
  }, [append, client, exit, overlay.confirm, slashCommands, ui.busy])

  const promptWidth = 3
  const inputColumns = Math.max(1, cols - promptWidth - 4)
  const completions = useMemo(() => {
    return semaSlashCompletions(slashCommands, input, 12)
  }, [input, slashCommands])

  const gateway = useMemo(() => ({
    gw: {request: (method: string, params?: Record<string, unknown>) => client.request(method, params)} as never,
    rpc: (method: string, params?: Record<string, unknown>) => client.request(method, params)
  }) as never, [client])

  const actions: AppLayoutActions = useMemo(() => ({
    answerApproval: () => {},
    answerClarify: () => {},
    answerConfirm: () => {},
    answerSecret: () => {},
    answerSudo: () => {},
    clearSelection: () => {},
    deleteSessionWithFallback: async () => false,
    onModelSelect: () => {},
    resumeById: () => {},
    setStickyPrompt: () => {}
  }), [])

  const composer: AppLayoutComposerProps = {
    cols,
    compIdx: Math.max(0, completions.length - 1),
    completions,
    empty: rows.length <= 1,




    handleTextPaste: () => null,
    input,
    inputBuf: [],
    pagerPageSize: Math.max(4, (stdout?.rows ?? 24) - 8),
    queueEditIdx: null,
    queuedDisplay: [],
    submit,
    updateInput: setInput,
    voiceRecordKey: DEFAULT_VOICE_RECORD_KEY
  }

  const status: AppLayoutStatusProps = {
    cwdLabel: String(initialized?.project?.root ?? process.cwd()),
    goodVibesTick: 0,
    persistentLine: formatSemaPersistentStatus({
      activeFocusTitle: ui.activeFocusTitle,
      changes: turnCodeChanges,
      contextPressure,
      responseTimeMs: turnStartedAt === null ? lastTurnDurationMs : Math.max(0, now - turnStartedAt)
    }),
    processActive: ui.busy || ui.bgTasks.size > 0,
    processLine: [setup && !setup.ltm_enabled ? 'LTM unavailable' : '', formatSemaProcessStatus(ui.busy ? ui.status : '', ui.bgTasks, ui.focusShiftNotice)].filter(Boolean).join(' | '),
    sessionStartedAt,
    showStickyPrompt: false,
    statusColor: ui.status === 'error' ? ui.theme.color.error : ui.busy ? ui.theme.color.warn : ui.theme.color.ok,
    stickyPrompt: '',
    turnStartedAt,
    voiceLabel: 'voice off'
  }

  const displayedRows = maint.active
    ? maint.historyItems.map((msg, index) => ({key: `ltm:${index}`, msg}))
    : rows
  const displayedVirtualRows = displayedRows.map((row, index) => ({index, key: row.key, msg: row.msg}))

  const transcript: AppLayoutTranscriptProps = {
    historyItems: displayedRows.map(row => row.msg),
    scrollRef,
    virtualHistory: {
      bottomSpacer: 0,
      end: displayedVirtualRows.length,
      measureRef: () => () => {},
      offsets: new Array(displayedVirtualRows.length + 1).fill(0),
      start: 0,
      topSpacer: 0
    },
    virtualRows: displayedVirtualRows
  }

  const progress: AppLayoutProgressProps = {showProgressArea: ui.busy}
  const semaOverlay = activeView === 'commandOutput' && commandOutput
    ? <CommandOutputPanel client={client} />
    : activeView === 'setup'
    ? <SetupPanel client={client} />
    : activeView === 'settings' && settings
    ? <SettingsPanel client={client} />
    : activeView === 'config' && config
      ? <ConfigPanel client={client} />
      : null
  const semaOverlayBlocksComposer = activeView !== 'commandOutput'

  return (
    <GatewayProvider value={gateway}>
      <AppLayout
        actions={actions}
        composer={composer}
        mouseTracking={ui.mouseTracking}
        progress={progress}
        semaClient={client}
        semaOverlay={semaOverlay}
        semaOverlayBlocksComposer={semaOverlayBlocksComposer}
        status={status}
        transcript={transcript}
      />
    </GatewayProvider>
  )
}
