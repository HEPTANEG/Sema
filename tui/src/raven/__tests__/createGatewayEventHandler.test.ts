




import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Msg } from '../types.js'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { getLtmMaintenanceState, openLtmMaintenanceView, resetLtmMaintenanceState } from '../app/ltmMaintenanceStore.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { estimateTokensRough } from '../lib/text.js'

const ref = <T>(current: T) => ({ current })

const buildCtx = (appended: Msg[]) =>
  ({
    composer: {
      dequeue: () => undefined,
      queueEditRef: ref<null | number>(null),
      sendQueued: vi.fn(),
      setInput: vi.fn()
    },
    gateway: {
      gw: { request: vi.fn() },
      rpc: vi.fn(async () => null)
    },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: ref(80),
      newSession: vi.fn(),
      resetSession: vi.fn(),
      resumeById: vi.fn(),
      setCatalog: vi.fn()
    },
    submission: {
      submitRef: { current: vi.fn() }
    },
    system: {
      bellOnComplete: false,
      sys: vi.fn()
    },
    transcript: {
      appendMessage: (msg: Msg) => appended.push(msg),
      panel: (title: string, sections: any[]) =>
        appended.push({ kind: 'panel', panelData: { sections, title }, role: 'system', text: '' }),
      setHistoryItems: vi.fn()
    },
    voice: {
      setProcessing: vi.fn(),
      setRecording: vi.fn(),
      setVoiceEnabled: vi.fn()
    }
  }) as any

describe('createGatewayEventHandler', () => {
  beforeEach(() => {
    resetOverlayState()
    resetLtmMaintenanceState()
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    patchUiState({ showReasoning: true })
  })

  it('renders LTM maintenance events into the dedicated live turn and releases busy on completion', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    openLtmMaintenanceView('/memory ltm maintain')
    patchUiState({ busy: true, sid: 'sess-1', status: 'maintaining LTM…' })

    onEvent({ payload: { run_id: 'run-1', start_status: 'running' }, type: 'ltm_maintenance_started' } as any)
    onEvent({
      payload: { run_id: 'run-1', stage: 'global_duplicate_review', stage_index: 1 },
      type: 'ltm_maintenance_stage_started'
    } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_model_turn' } as any)
    onEvent({
      payload: { args: { projection: 'bundle' }, item_id: 'tool-1', run_id: 'run-1', tool: 'get_object' },
      type: 'ltm_maintenance_tool_started'
    } as any)
    onEvent({
      payload: { item_id: 'tool-1', result: { count: 1 }, run_id: 'run-1', status: 'ok', tool: 'get_object' },
      type: 'ltm_maintenance_tool_finished'
    } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_final_check_started' } as any)
    onEvent({
      payload: {
        final_check_status: 'passed',
        run_id: 'run-1',
        state: { maintenance_coverage_cursor: 3 },
        status: 'completed'
      },
      type: 'ltm_maintenance_completed'
    } as any)

    const state = getLtmMaintenanceState()
    expect(getUiState().busy).toBe(false)
    expect(state).toMatchObject({
      active: true,
      runId: 'run-1',
      stage: 'global_duplicate_review',
      stageIndex: 1,
      status: 'completed',
      terminal: true
    })
    expect(state.historyItems.some(msg => msg.kind === 'episodes')).toBe(true)
    expect(state.summary).toContain('coverage_cursor: 3')
    expect(appended).toEqual([])
  })

  it('opens the dedicated maintenance view when the first event arrives', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    onEvent({ payload: { run_id: 'run-1', start_status: 'running' }, type: 'ltm_maintenance_started' } as any)

    expect(getLtmMaintenanceState().active).toBe(true)
    expect(getLtmMaintenanceState().historyItems[0]).toMatchObject({kind: 'slash', text: '/memory ltm maintain'})
  })

  it('closes the maintenance view when a task-resume wake is delivered', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    openLtmMaintenanceView('/memory ltm maintain')
    patchUiState({ busy: false, status: 'LTM Maintenance completed' })

    onEvent({
      payload: { kind: 'maintenance_task_resumed', source: 'maintainer', wake_id: 'wake-1' },
      type: 'wake_delivered'
    } as any)

    expect(getLtmMaintenanceState().active).toBe(false)
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('resuming task after LTM maintenance…')
  })

  it('returns to Main Agent when a maintenance-failure wake is delivered', () => {
    const onEvent = createGatewayEventHandler(buildCtx([]))

    openLtmMaintenanceView('/memory ltm maintain')
    patchUiState({ busy: false, status: 'LTM Maintenance failed' })

    onEvent({
      payload: { kind: 'maintenance_sleep_failed', source: 'maintainer', wake_id: 'wake-failed-1' },
      type: 'wake_delivered'
    } as any)

    expect(getLtmMaintenanceState().active).toBe(false)
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('handling LTM maintenance failure…')
  })

  it('shows the first rejected LTM maintenance plan error in the tool summary', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    openLtmMaintenanceView('/memory ltm maintain')
    patchUiState({ busy: true, sid: 'sess-1', status: 'maintaining LTM…' })

    onEvent({ payload: { run_id: 'run-1', start_status: 'running' }, type: 'ltm_maintenance_started' } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_model_turn' } as any)
    onEvent({
      payload: { args: { intents: [{ intent_type: 'create_object' }] }, item_id: 'plan-1', run_id: 'run-1', tool: 'submit_maintenance_plan' },
      type: 'ltm_maintenance_tool_started'
    } as any)
    onEvent({
      payload: {
        item_id: 'plan-1',
        result: { errors: [{ code: 'compile_error', message: 'unsupported intent_type: create_object' }], status: 'rejected' },
        run_id: 'run-1',
        status: 'rejected',
        tool: 'submit_maintenance_plan'
      },
      type: 'ltm_maintenance_tool_finished'
    } as any)

    expect(getTurnState().episodes[0]?.tools[0]).toMatchObject({
      done: true,
      ok: true,
      resultPreview: 'plan rejected: compile_error: unsupported intent_type: create_object'
    })
  })

  it('shows retrieval technical failures as failed tools without ending maintenance', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    openLtmMaintenanceView('/memory ltm maintain')
    patchUiState({ busy: true, sid: 'sess-1', status: 'maintaining LTM…' })
    onEvent({ type: 'ltm_maintenance_started', payload: { run_id: 'run-1' } } as any)
    onEvent({ type: 'ltm_maintenance_model_turn', payload: { run_id: 'run-1' } } as any)
    onEvent({ type: 'ltm_maintenance_tool_started', payload: { run_id: 'run-1', tool: 'retrieve_memory', item_id: 'ret-1', args: { query: 'topic' } } } as any)
    onEvent({ type: 'ltm_maintenance_tool_finished', payload: { run_id: 'run-1', tool: 'retrieve_memory', item_id: 'ret-1', status: 'error', result: { kind: 'technical', message: 'offline' } } } as any)
    expect(getTurnState().episodes[0]?.tools[0]).toMatchObject({ done: true, ok: false, resultPreview: 'retrieval technical: offline' })
    expect(appended).toEqual([])
  })

  it('shows LTM maintenance model retry errors with diagnostics', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    openLtmMaintenanceView('/memory ltm maintain')
    patchUiState({ busy: true, sid: 'sess-1', status: 'maintaining LTM…' })

    onEvent({ payload: { run_id: 'run-1', start_status: 'running' }, type: 'ltm_maintenance_started' } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_model_turn' } as any)
    onEvent({
      payload: {
        diagnostics: { message_count: 4, tool_result_chars: 128, tool_schema_count: 7 },
        error: 'Could not reach deepseek',
        max_retries: 2,
        retry: 0,
        run_id: 'run-1'
      },
      type: 'ltm_maintenance_model_error'
    } as any)

    expect(getTurnState().episodes[0]?.tools[0]).toMatchObject({
      done: true,
      ok: false,
      name: 'model_retry',
      resultPreview: 'Could not reach deepseek'
    })
  })

  it('renders LTM maintenance reasoning and content into the dedicated episode only', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    openLtmMaintenanceView('/memory ltm maintain')
    patchUiState({ busy: true, sid: 'sess-1', status: 'maintaining LTM…' })

    onEvent({ payload: { run_id: 'run-1', start_status: 'running' }, type: 'ltm_maintenance_started' } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_model_turn' } as any)
    onEvent({
      payload: { content: 'Checking the maintenance frontier.', run_id: 'run-1' },
      type: 'ltm_maintenance_reasoning_available'
    } as any)
    onEvent({
      payload: { content: 'I will inspect linked objects before writing.', run_id: 'run-1' },
      type: 'ltm_maintenance_message_delta'
    } as any)
    onEvent({
      payload: { args: { object_ids: ['mem_1'] }, item_id: 'tool-1', run_id: 'run-1', tool: 'get_links' },
      type: 'ltm_maintenance_tool_started'
    } as any)

    const episode = getTurnState().episodes[0]
    expect(episode?.reasoning).toContain('Checking the maintenance frontier.')
    expect(episode?.narration).toContain('I will inspect linked objects before writing.')
    expect(episode?.tools[0]).toMatchObject({ name: 'get_links' })
    expect(appended).toEqual([])
  })

  it.each(['ltm_maintenance_repetition_warning', 'ltm_maintenance_runtime_notice'])('shows %s only in maintenance and releases busy on failure', (noticeType) => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    openLtmMaintenanceView('/memory ltm maintain')
    patchUiState({ busy: true, sid: 'sess-1', status: 'maintaining LTM…' })
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_started' } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_model_turn' } as any)
    onEvent({ payload: { run_id: 'run-1', content: 'Re-examine your current behavior.', repeated_rounds: 3 }, type: noticeType } as any)
    expect(getTurnState().episodes[0]?.narration).toContain('Runtime warning: Re-examine your current behavior.')
    expect(appended).toEqual([])
    onEvent({ payload: { run_id: 'run-1', failure_reason: 'repeated_observation_no_progress', status: 'failed' }, type: 'ltm_maintenance_failed' } as any)
    expect(getUiState().busy).toBe(false)
    expect(getLtmMaintenanceState()).toMatchObject({ status: 'failed', terminal: true })
    expect(appended).toEqual([])
  })

  it('closes repeated LTM maintenance final checks in their own episodes', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    openLtmMaintenanceView('/memory ltm maintain')
    patchUiState({ busy: true, sid: 'sess-1', status: 'maintaining LTM…' })

    onEvent({ payload: { run_id: 'run-1', start_status: 'running' }, type: 'ltm_maintenance_started' } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_model_turn' } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_final_check_started' } as any)
    onEvent({
      payload: { health: { derived: { missing: 1 }, status: 'error' }, run_id: 'run-1' },
      type: 'ltm_maintenance_final_check_failed'
    } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_model_turn' } as any)
    onEvent({ payload: { run_id: 'run-1' }, type: 'ltm_maintenance_final_check_started' } as any)

    const liveFinalChecks = getTurnState().episodes.flatMap(ep => ep.tools.filter(tool => tool.name === 'final_check'))
    expect(liveFinalChecks).toHaveLength(2)
    expect(new Set(liveFinalChecks.map(tool => tool.id)).size).toBe(2)
    expect(liveFinalChecks.map(tool => tool.done)).toEqual([true, false])
    expect(liveFinalChecks.map(tool => tool.ok)).toEqual([false, true])
    expect(liveFinalChecks[0]?.resultPreview).toBe('final check failed')

    onEvent({
      payload: {
        final_check_status: 'passed',
        run_id: 'run-1',
        state: { maintenance_coverage_cursor: 785 },
        status: 'completed'
      },
      type: 'ltm_maintenance_completed'
    } as any)

    expect(getLtmMaintenanceState()).toMatchObject({ status: 'completed', terminal: true })
  })

  it('archives incomplete todos into transcript flow at end of turn so they scroll up', () => {
    const appended: Msg[] = []

    const todos = [
      { content: 'Gather ingredients', id: 'prep', status: 'completed' },
      { content: 'Boil water', id: 'boil', status: 'in_progress' },
      { content: 'Make sauce', id: 'sauce', status: 'pending' }
    ]

    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: {}, type: 'message.start' } as any)
    onEvent({ payload: { name: 'todo', todos, tool_id: 'todo-1' }, type: 'tool.start' } as any)
    expect(getTurnState().todos).toEqual(todos)

    onEvent({ payload: { text: 'Started a todo list.' }, type: 'message.complete' } as any)

    const trail = appended.find(msg => msg.kind === 'trail' && msg.todos?.length)
    const finalText = appended.find(msg => msg.role === 'assistant' && msg.text === 'Started a todo list.')

    expect(finalText).toBeDefined()
    expect(trail).toMatchObject({ kind: 'trail', role: 'system', todos, todoIncomplete: true })


    expect(appended.indexOf(trail!)).toBeLessThan(appended.indexOf(finalText!))
    expect(getTurnState().todos).toEqual([])
  })

  it('archives completed todos into transcript flow at end of turn', () => {
    const appended: Msg[] = []
    const todos = [{ content: 'Serve tiny latte', id: 'serve', status: 'completed' }]
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { name: 'todo', todos, tool_id: 'todo-1' }, type: 'tool.start' } as any)
    onEvent({ payload: { text: 'done' }, type: 'message.complete' } as any)

    expect(getTurnState().todos).toEqual([])
    expect(appended).toContainEqual({
      kind: 'trail',
      role: 'system',
      text: '',
      todoCollapsedByDefault: true,
      todos
    })
  })

  it('keeps the current todo list visible when the next message starts', () => {
    const appended: Msg[] = []
    const todos = [{ content: 'Boil water', id: 'boil', status: 'in_progress' }]

    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { name: 'todo', todos, tool_id: 'todo-1' }, type: 'tool.start' } as any)
    expect(getTurnState().todos).toEqual(todos)

    onEvent({ payload: {}, type: 'message.start' } as any)

    expect(getTurnState().todos).toEqual(todos)
  })

  it('prints compaction progress status into the transcript', () => {
    const appended: Msg[] = []
    const ctx = buildCtx(appended)
    const onEvent = createGatewayEventHandler(ctx)

    onEvent({
      payload: { kind: 'compressing', text: 'compressing 968 messages (~123,400 tok)…' },
      type: 'status.update'
    } as any)

    expect(ctx.system.sys).toHaveBeenCalledWith('compressing 968 messages (~123,400 tok)…')
  })

  it('ignores review.summary events with empty or missing text', () => {
    const appended: Msg[] = []
    const ctx = buildCtx(appended)
    const onEvent = createGatewayEventHandler(ctx)

    onEvent({ payload: { text: '' }, type: 'review.summary' } as any)
    onEvent({ payload: { text: '   ' }, type: 'review.summary' } as any)
    onEvent({ payload: undefined, type: 'review.summary' } as any)

    expect(ctx.system.sys).not.toHaveBeenCalled()
  })

  it('clears the visible todo list when the todo tool returns an empty list', () => {
    const appended: Msg[] = []
    const todos = [{ content: 'Boil water', id: 'boil', status: 'in_progress' }]
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { name: 'todo', todos, tool_id: 'todo-1' }, type: 'tool.start' } as any)
    expect(getTurnState().todos).toEqual(todos)

    onEvent({ payload: { name: 'todo', todos: [], tool_id: 'todo-1' }, type: 'tool.complete' } as any)

    expect(getTurnState().todos).toEqual([])
  })

  it('persists completed tool rows when message.complete lands immediately after tool.complete', () => {
    const appended: Msg[] = []

    turnController.reasoningText = 'mapped the page'
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({
      payload: { context: 'home page', name: 'search', tool_id: 'tool-1' },
      type: 'tool.start'
    } as any)
    onEvent({
      payload: { name: 'search', preview: 'hero cards' },
      type: 'tool.progress'
    } as any)
    onEvent({
      payload: { summary: 'done', tool_id: 'tool-1' },
      type: 'tool.complete'
    } as any)
    onEvent({
      payload: { text: 'final answer' },
      type: 'message.complete'
    } as any)

    expect(appended).toHaveLength(2)
    expect(appended[0]).toMatchObject({ kind: 'trail', role: 'system', text: '', thinking: 'mapped the page' })
    expect(appended[0]?.tools).toHaveLength(1)
    expect(appended[0]?.tools?.[0]).toContain('hero cards')
    expect(appended[0]?.toolTokens).toBeGreaterThan(0)
    expect(appended[1]).toMatchObject({ role: 'assistant', text: 'final answer' })
  })

  it('groups sequential completed tools into one trail when the turn completes', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { context: 'alpha', name: 'search_files', tool_id: 'tool-1' }, type: 'tool.start' } as any)
    onEvent({
      payload: { name: 'search_files', summary: 'first done', tool_id: 'tool-1' },
      type: 'tool.complete'
    } as any)
    onEvent({ payload: { context: 'beta', name: 'read_file', tool_id: 'tool-2' }, type: 'tool.start' } as any)
    onEvent({ payload: { name: 'read_file', summary: 'second done', tool_id: 'tool-2' }, type: 'tool.complete' } as any)

    expect(getTurnState().streamSegments.filter(msg => msg.kind === 'trail' && msg.tools?.length)).toHaveLength(1)
    expect(getTurnState().streamSegments[0]?.tools).toHaveLength(2)
    expect(getTurnState().streamPendingTools).toEqual([])

    onEvent({ payload: { text: '' }, type: 'message.complete' } as any)

    const toolTrails = appended.filter(msg => msg.kind === 'trail' && msg.tools?.length)
    expect(toolTrails).toHaveLength(1)
    expect(toolTrails[0]?.tools).toHaveLength(2)
    expect(toolTrails[0]?.tools?.[0]).toContain('Search Files')
    expect(toolTrails[0]?.tools?.[1]).toContain('Read File')
  })

  it('keeps tool tokens across handler recreation mid-turn', () => {
    const appended: Msg[] = []

    turnController.reasoningText = 'mapped the page'

    createGatewayEventHandler(buildCtx(appended))({
      payload: { context: 'home page', name: 'search', tool_id: 'tool-1' },
      type: 'tool.start'
    } as any)

    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({
      payload: { name: 'search', preview: 'hero cards' },
      type: 'tool.progress'
    } as any)
    onEvent({
      payload: { summary: 'done', tool_id: 'tool-1' },
      type: 'tool.complete'
    } as any)
    onEvent({
      payload: { text: 'final answer' },
      type: 'message.complete'
    } as any)

    expect(appended).toHaveLength(2)
    expect(appended[0]?.tools).toHaveLength(1)
    expect(appended[0]?.toolTokens).toBeGreaterThan(0)
    expect(appended[1]).toMatchObject({ role: 'assistant', text: 'final answer' })
  })

  it('streams legacy thinking.delta into visible reasoning state', () => {
    vi.useFakeTimers()
    const appended: Msg[] = []
    const streamed = 'short streamed reasoning'

    createGatewayEventHandler(buildCtx(appended))({ payload: { text: streamed }, type: 'thinking.delta' } as any)
    vi.runOnlyPendingTimers()

    expect(getTurnState().reasoning).toBe(streamed)
    expect(getTurnState().reasoningActive).toBe(true)
    expect(getTurnState().reasoningTokens).toBe(estimateTokensRough(streamed))
    vi.useRealTimers()
  })

  it('preserves streamed reasoning as one completed thinking panel after segment flushes', () => {
    const appended: Msg[] = []
    const streamed = 'first reasoning chunk\nsecond reasoning chunk'

    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { text: streamed }, type: 'reasoning.delta' } as any)
    onEvent({ payload: { text: 'Before edit.' }, type: 'message.delta' } as any)
    turnController.flushStreamingSegment()
    onEvent({ payload: { text: 'final answer' }, type: 'message.complete' } as any)

    expect(appended.map(msg => msg.thinking).filter(Boolean)).toEqual([streamed])
    expect(appended[appended.length - 1]).toMatchObject({ role: 'assistant', text: 'final answer' })
  })

  it('filters spinner/status-only reasoning noise from completed thinking', () => {
    const appended: Msg[] = []
    const streamed = '(¬_¬) synthesizing...\nactual plan\n( ͡° ͜ʖ ͡°) pondering...\nnext step'

    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { text: streamed }, type: 'reasoning.delta' } as any)
    onEvent({ payload: { text: 'final answer' }, type: 'message.complete' } as any)

    expect(appended[0]?.thinking).toBe(streamed)
    expect(appended[0]?.text).toBe('')
    expect(appended[appended.length - 1]).toMatchObject({ role: 'assistant', text: 'final answer' })
  })

  it('ignores fallback reasoning.available when streamed reasoning already exists', () => {
    const appended: Msg[] = []
    const streamed = 'short streamed reasoning'
    const fallback = 'x'.repeat(400)

    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { text: streamed }, type: 'reasoning.delta' } as any)
    onEvent({ payload: { text: fallback }, type: 'reasoning.available' } as any)
    onEvent({ payload: { text: 'final answer' }, type: 'message.complete' } as any)

    expect(appended).toHaveLength(2)
    expect(appended[0]?.thinking).toBe(streamed)
    expect(appended[0]?.thinkingTokens).toBe(estimateTokensRough(streamed))
    expect(appended[1]).toMatchObject({ role: 'assistant', text: 'final answer' })
  })

  it('uses message.complete reasoning when no streamed reasoning ref', () => {
    const appended: Msg[] = []
    const fromServer = 'recovered from last_reasoning'

    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { reasoning: fromServer, text: 'final answer' }, type: 'message.complete' } as any)

    expect(appended).toHaveLength(2)
    expect(appended[0]?.thinking).toBe(fromServer)
    expect(appended[0]?.thinkingTokens).toBe(estimateTokensRough(fromServer))
    expect(appended[1]).toMatchObject({ role: 'assistant', text: 'final answer' })
  })

  it('renders browser.progress events as system transcript lines as they stream in', () => {
    const appended: Msg[] = []
    const ctx = buildCtx(appended)
    const handler = createGatewayEventHandler(ctx)

    handler({
      payload: { message: 'Chrome launched and listening on port 9222' },
      type: 'browser.progress'
    } as any)

    expect(ctx.system.sys).toHaveBeenCalledWith('Chrome launched and listening on port 9222')
  })

  it('annotates gateway.start_timeout with stderr tail lines so users can diagnose without /logs', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({
      payload: {
        cwd: '/repo',
        python: '/opt/venv/bin/python',
        stderr_tail:
          '[startup] timed out\nModuleNotFoundError: No module named openai\nFileNotFoundError: ~/.raven/config.yaml'
      },
      type: 'gateway.start_timeout'
    } as any)

    const messages = getTurnState().activity.map(a => a.text)

    expect(messages.some(m => m.includes('gateway startup timed out'))).toBe(true)
    expect(messages.some(m => m.includes('ModuleNotFoundError'))).toBe(true)
    expect(messages.some(m => m.includes('FileNotFoundError'))).toBe(true)
  })

  it('prefers raw text over Rich-rendered ANSI on message.complete (#16391)', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    const raw = 'Raven here.\n\nLine two.'


    const rendered = '\u001b[33mRaven here.\u001b[0m\n\n\u001b[2mLine two.\u001b[0m'

    onEvent({ payload: { rendered, text: raw }, type: 'message.complete' } as any)

    const assistant = appended.find(msg => msg.role === 'assistant')
    expect(assistant?.text).toBe(raw)
    expect(assistant?.text).not.toContain('\u001b[')
  })

  it('falls back to payload.rendered when text is missing on message.complete', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    const rendered = 'fallback when gateway omitted text'

    onEvent({ payload: { rendered }, type: 'message.complete' } as any)

    const assistant = appended.find(msg => msg.role === 'assistant')
    expect(assistant?.text).toBe(rendered)
  })

  it('always accumulates raw text in message.delta and ignores `rendered` (#16391)', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))




    onEvent({ payload: { rendered: '\u001b[33mFi\u001b[0m', text: 'Fi' }, type: 'message.delta' } as any)
    onEvent({ payload: { rendered: '\u001b[33mrst.\u001b[0m', text: 'rst.' }, type: 'message.delta' } as any)
    onEvent({ payload: { text: ' second.' }, type: 'message.delta' } as any)
    onEvent({ payload: {}, type: 'message.complete' } as any)

    const assistant = appended.find(msg => msg.role === 'assistant')
    expect(assistant?.text).toBe('First. second.')
  })

  it('anchors inline_diff as its own segment where the edit happened', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    const diff = '\u001b[31m--- a/foo.ts\u001b[0m\n\u001b[32m+++ b/foo.ts\u001b[0m\n@@\n-old\n+new'
    const cleaned = '--- a/foo.ts\n+++ b/foo.ts\n@@\n-old\n+new'
    const block = `\`\`\`diff\n${cleaned}\n\`\`\``




    onEvent({ payload: { text: 'Editing the file' }, type: 'message.delta' } as any)
    onEvent({ payload: { context: 'foo.ts', name: 'patch', tool_id: 'tool-1' }, type: 'tool.start' } as any)
    onEvent({ payload: { inline_diff: diff, summary: 'patched', tool_id: 'tool-1' }, type: 'tool.complete' } as any)


    expect(appended).toHaveLength(0)
    expect(turnController.segmentMessages).toEqual([
      { role: 'assistant', text: 'Editing the file' },
      {
        kind: 'diff',
        role: 'assistant',
        text: block,
        tools: [expect.stringMatching(/^Patch\("foo\.ts"\)(?: \([^)]+\))? ✓$/)]
      }
    ])

    onEvent({ payload: { text: 'patch applied' }, type: 'message.complete' } as any)

    expect(appended).toHaveLength(4)
    expect(appended[0]?.text).toBe('Editing the file')
    expect(appended[1]).toMatchObject({ kind: 'diff', text: block })
    expect(appended[1]?.tools?.[0]).toContain('Patch')
    expect(appended[3]?.text).toBe('patch applied')
    expect(appended[3]?.text).not.toContain('```diff')
  })

  it('keeps full final responses from duplicating flushed pre-diff narration', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    const diff = '--- a/foo.ts\n+++ b/foo.ts\n@@\n-old\n+new'
    const block = `\`\`\`diff\n${diff}\n\`\`\``

    onEvent({ payload: { text: 'Before edit. ' }, type: 'message.delta' } as any)
    onEvent({ payload: { context: 'foo.ts', name: 'patch', tool_id: 'tool-1' }, type: 'tool.start' } as any)
    onEvent({ payload: { inline_diff: diff, summary: 'patched', tool_id: 'tool-1' }, type: 'tool.complete' } as any)
    onEvent({ payload: { text: 'After edit.' }, type: 'message.delta' } as any)
    onEvent({ payload: { text: 'Before edit. After edit.' }, type: 'message.complete' } as any)

    expect(appended.map(msg => msg.text.trim()).filter(Boolean)).toEqual(['Before edit.', block, 'After edit.'])
    expect(appended[1]?.tools?.[0]).toContain('Patch')
  })

  it('drops the diff segment when the final assistant text narrates the same diff', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    const cleaned = '--- a/foo.ts\n+++ b/foo.ts\n@@\n-old\n+new'
    const assistantText = `Done. Here's the inline diff:\n\n\`\`\`diff\n${cleaned}\n\`\`\``

    onEvent({ payload: { inline_diff: cleaned, summary: 'patched', tool_id: 'tool-1' }, type: 'tool.complete' } as any)
    onEvent({ payload: { text: assistantText }, type: 'message.complete' } as any)



    expect(appended).toHaveLength(1)
    expect(appended[0]?.text).toBe(assistantText)
    expect((appended[0]?.text.match(/```diff/g) ?? []).length).toBe(1)
  })

  it('strips the CLI "┊ review diff" header from inline diff segments', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    const raw = '  \u001b[33m┊ review diff\u001b[0m\n--- a/foo.ts\n+++ b/foo.ts\n@@\n-old\n+new'

    onEvent({ payload: { inline_diff: raw, summary: 'patched', tool_id: 'tool-1' }, type: 'tool.complete' } as any)
    onEvent({ payload: { text: 'done' }, type: 'message.complete' } as any)


    expect(appended).toHaveLength(2)
    expect(appended[0]?.kind).toBe('diff')
    expect(appended[0]?.text).not.toContain('┊ review diff')
    expect(appended[0]?.text).toContain('--- a/foo.ts')
    expect(appended[0]?.tools?.[0]).toContain('Tool')
    expect(appended[1]?.text).toBe('done')
  })

  it('drops the diff segment when assistant writes its own ```diff fence', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    const inlineDiff = '--- a/foo.ts\n+++ b/foo.ts\n@@\n-old\n+new'
    const assistantText = 'Done. Clean swap:\n\n```diff\n-old\n+new\n```'

    onEvent({
      payload: { inline_diff: inlineDiff, summary: 'patched', tool_id: 'tool-1' },
      type: 'tool.complete'
    } as any)
    onEvent({ payload: { text: assistantText }, type: 'message.complete' } as any)

    expect(appended).toHaveLength(1)
    expect(appended[0]?.text).toBe(assistantText)
    expect((appended[0]?.text.match(/```diff/g) ?? []).length).toBe(1)
  })

  it('keeps tool trail terse when inline_diff is present', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))
    const diff = '--- a/foo.ts\n+++ b/foo.ts\n@@\n-old\n+new'

    onEvent({
      payload: { inline_diff: diff, name: 'review_diff', summary: diff, tool_id: 'tool-1' },
      type: 'tool.complete'
    } as any)
    onEvent({ payload: { text: 'done' }, type: 'message.complete' } as any)



    expect(appended).toHaveLength(2)
    expect(appended[0]?.kind).toBe('diff')
    expect(appended[0]?.text).toContain('```diff')
    expect(appended[0]?.tools?.[0]).toContain('Review Diff')
    expect(appended[0]?.tools?.[0]).not.toContain('--- a/foo.ts')
    expect(appended[1]?.text).toBe('done')
    expect(appended[1]?.tools ?? []).toEqual([])
  })

  it('shows setup panel for missing provider startup error', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({
      payload: {
        message:
          'agent init failed: No LLM provider configured. Run `raven model` to select a provider, or run `raven setup` for first-time configuration.'
      },
      type: 'error'
    } as any)

    expect(appended).toHaveLength(1)
    expect(appended[0]).toMatchObject({
      kind: 'panel',
      panelData: { title: 'Setup Required' },
      role: 'system'
    })
  })

  it('on gateway.ready with no STARTUP_RESUME_ID and auto_resume off, forges a new session', async () => {
    const appended: Msg[] = []
    const newSession = vi.fn()
    const resumeById = vi.fn()
    const ctx = buildCtx(appended)

    ctx.session.newSession = newSession
    ctx.session.resumeById = resumeById
    ctx.session.STARTUP_RESUME_ID = ''
    ctx.gateway.rpc = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        return { config: { display: { tui_auto_resume_recent: false } } }
      }

      return null
    })

    createGatewayEventHandler(ctx)({ payload: {}, type: 'gateway.ready' } as any)

    await vi.waitFor(() => expect(newSession).toHaveBeenCalled())
    expect(resumeById).not.toHaveBeenCalled()
  })

  it('on gateway.ready with auto_resume on and a recent session, resumes it', async () => {
    const appended: Msg[] = []
    const newSession = vi.fn()
    const resumeById = vi.fn()
    const ctx = buildCtx(appended)

    ctx.session.newSession = newSession
    ctx.session.resumeById = resumeById
    ctx.session.STARTUP_RESUME_ID = ''
    ctx.gateway.rpc = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        return { config: { display: { tui_auto_resume_recent: true } } }
      }

      if (method === 'session.most_recent') {
        return { session_id: 'sess-most-recent' }
      }

      return null
    })

    createGatewayEventHandler(ctx)({ payload: {}, type: 'gateway.ready' } as any)

    await vi.waitFor(() => expect(resumeById).toHaveBeenCalledWith('sess-most-recent'))
    expect(newSession).not.toHaveBeenCalled()
  })

  it('on gateway.ready with auto_resume on but no eligible session, falls back to new', async () => {
    const appended: Msg[] = []
    const newSession = vi.fn()
    const resumeById = vi.fn()
    const ctx = buildCtx(appended)

    ctx.session.newSession = newSession
    ctx.session.resumeById = resumeById
    ctx.session.STARTUP_RESUME_ID = ''
    ctx.gateway.rpc = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        return { config: { display: { tui_auto_resume_recent: true } } }
      }

      if (method === 'session.most_recent') {
        return { session_id: null }
      }

      return null
    })

    createGatewayEventHandler(ctx)({ payload: {}, type: 'gateway.ready' } as any)

    await vi.waitFor(() => expect(newSession).toHaveBeenCalled())
    expect(resumeById).not.toHaveBeenCalled()
  })

  it('on gateway.ready when config.get rejects, falls back to new session', async () => {
    const appended: Msg[] = []
    const newSession = vi.fn()
    const resumeById = vi.fn()
    const ctx = buildCtx(appended)

    ctx.session.newSession = newSession
    ctx.session.resumeById = resumeById
    ctx.session.STARTUP_RESUME_ID = ''
    ctx.gateway.rpc = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        throw new Error('gateway timeout')
      }

      return null
    })

    createGatewayEventHandler(ctx)({ payload: {}, type: 'gateway.ready' } as any)

    await vi.waitFor(() => expect(newSession).toHaveBeenCalled())
    expect(resumeById).not.toHaveBeenCalled()
  })

  it('on gateway.ready when session.most_recent rejects, falls back to new session', async () => {
    const appended: Msg[] = []
    const newSession = vi.fn()
    const resumeById = vi.fn()
    const ctx = buildCtx(appended)

    ctx.session.newSession = newSession
    ctx.session.resumeById = resumeById
    ctx.session.STARTUP_RESUME_ID = ''
    ctx.gateway.rpc = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        return { config: { display: { tui_auto_resume_recent: true } } }
      }

      if (method === 'session.most_recent') {
        throw new Error('db locked')
      }

      return null
    })

    createGatewayEventHandler(ctx)({ payload: {}, type: 'gateway.ready' } as any)

    await vi.waitFor(() => expect(newSession).toHaveBeenCalled())
    expect(resumeById).not.toHaveBeenCalled()
  })

  it('on gateway.ready with STARTUP_RESUME_ID set, the env wins over config auto_resume', async () => {
    const appended: Msg[] = []
    const newSession = vi.fn()
    const resumeById = vi.fn()
    const ctx = buildCtx(appended)

    ctx.session.newSession = newSession
    ctx.session.resumeById = resumeById
    ctx.session.STARTUP_RESUME_ID = 'env-explicit'
    ctx.gateway.rpc = vi.fn(async () => ({
      config: { display: { tui_auto_resume_recent: true } }
    }))

    createGatewayEventHandler(ctx)({ payload: {}, type: 'gateway.ready' } as any)

    await vi.waitFor(() => expect(resumeById).toHaveBeenCalledWith('env-explicit'))
    expect(newSession).not.toHaveBeenCalled()
  })

  it('keeps gateway noise informational and approval out of Activity', async () => {
    const appended: Msg[] = []
    const ctx = buildCtx(appended)
    ctx.gateway.rpc = vi.fn(async () => {
      throw new Error('cold start')
    })

    const onEvent = createGatewayEventHandler(ctx)

    onEvent({ payload: { line: 'Traceback: noisy but non-fatal' }, type: 'gateway.stderr' } as any)
    onEvent({ payload: { preview: 'bad framing' }, type: 'gateway.protocol_error' } as any)
    onEvent({
      payload: { command: 'rm -rf /tmp/nope', description: 'dangerous command' },
      type: 'approval.request'
    } as any)
    onEvent({ payload: {}, type: 'gateway.ready' } as any)

    expect(getOverlayState().approval).toMatchObject({ description: 'dangerous command' })
    await vi.waitFor(() => {
      expect(getTurnState().activity).toMatchObject([
        { text: 'Traceback: noisy but non-fatal', tone: 'info' },
        { text: 'protocol noise detected · /logs to inspect', tone: 'info' },
        { text: 'protocol noise: bad framing', tone: 'info' },
        { text: 'command catalog unavailable: cold start', tone: 'info' }
      ])
    })
  })

  it('still surfaces terminal turn failures as errors', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: { message: 'boom' }, type: 'error' } as any)

    expect(getTurnState().activity).toMatchObject([{ text: 'boom', tone: 'error' }])
  })

  it('drops stale reasoning/tool/todos events after ctrl-c until the next message starts', () => {








    vi.useFakeTimers()

    try {
      const appended: Msg[] = []
      const ctx = buildCtx(appended)
      ctx.gateway.gw.request = vi.fn(async () => ({ status: 'interrupted' }))
      const onEvent = createGatewayEventHandler(ctx)

      patchUiState({ sid: 'sess-1' })
      onEvent({ payload: {}, type: 'message.start' } as any)
      onEvent({
        payload: {
          context: 'pre',
          name: 'search',
          todos: [{ content: 'pre-interrupt', id: 'todo-1', status: 'pending' }],
          tool_id: 't-1'
        },
        type: 'tool.start'
      } as any)


      expect(getTurnState().todos).toEqual([{ content: 'pre-interrupt', id: 'todo-1', status: 'pending' }])

      turnController.interruptTurn({
        appendMessage: (msg: Msg) => appended.push(msg),
        gw: ctx.gateway.gw,
        sid: 'sess-1',
        sys: ctx.system.sys
      })

      onEvent({ payload: { text: 'still thinking…' }, type: 'reasoning.delta' } as any)

      onEvent({
        payload: {
          context: 'post',
          name: 'browser',
          todos: [{ content: 'late ghost', id: 'todo-ghost', status: 'pending' }],
          tool_id: 't-2'
        },
        type: 'tool.start'
      } as any)

      const trailBefore = getTurnState().turnTrail.length
      onEvent({ payload: { name: 'browser' }, type: 'tool.generating' } as any)
      expect(getTurnState().turnTrail.length).toBe(trailBefore)
      onEvent({ payload: { name: 'browser', preview: 'loading' }, type: 'tool.progress' } as any)
      onEvent({ payload: { summary: 'done', tool_id: 't-2' }, type: 'tool.complete' } as any)
      onEvent({ payload: { text: 'late chunk' }, type: 'message.delta' } as any)

      expect(getTurnState().tools).toEqual([])
      expect(turnController.reasoningText).toBe('')
      expect(turnController.bufRef).toBe('')
      expect(getTurnState().streamPendingTools).toEqual([])
      expect(getTurnState().streamSegments).toEqual([])



      expect(getTurnState().todos.find(t => t.content === 'late ghost')).toBeUndefined()

      onEvent({ payload: {}, type: 'message.start' } as any)
      onEvent({ payload: { text: 'fresh' }, type: 'reasoning.delta' } as any)

      expect(turnController.reasoningText).toBe('fresh')
    } finally {



      vi.runAllTimers()
      vi.useRealTimers()
    }
  })
})
