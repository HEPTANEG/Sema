import {beforeEach, describe, expect, it, vi} from 'vitest';
import {
  $activeView,
  $backgroundActivities,
  $busy,
  $cancelRequested,
  $commandOutput,
  $config,
  $continuedSession,
  $contextPressure,
  $foregroundStatus,
  $lastEventAt,
  $lastTurnDurationMs,
  $notifications,
  $operationStartedAt,
  $pendingConfirmation,
  $pendingUserMessage,
  $liveItems,
  $staticLogItems,
  $streamingAssistantBuffers,
  $status,
  $transcriptFollow,
  $transcriptScroll,
  $tools,
  $transcript,
  $turnCodeChanges,
  $turnStartedAt,
  applyBridgeEvent,
  applyInitialize,
  appendTranscript,
  cancelTurn,
  openLatestToolDetail,
  registerTranscriptScrollController,
  respondToPendingConfirmation,
  scrollTranscriptBy,
  scrollTranscriptTo,
  finishTurnTimer,
  startTurnTimer,
  submitPrompt
} from './runtime.js';
import {resetNotificationsForTests, setNotifierForTests, setTerminalFocusedForTests} from '../notifications.js';

describe('runtime stores', () => {
  beforeEach(() => {
    $status.set('starting');
    $backgroundActivities.set([]);
    $foregroundStatus.set('');
    $staticLogItems.set([]);
    $liveItems.set([]);
    $streamingAssistantBuffers.set({});
    $transcript.set([]);
    $tools.set([]);
    $turnCodeChanges.set({added: 0, removed: 0, files: []});
    $contextPressure.set(null);
    $pendingConfirmation.set(null);
    $pendingUserMessage.set(null);
    $commandOutput.set(null);
    $config.set(null);
    $continuedSession.set(null);
    $activeView.set('chat');
    $busy.set(false);
    $operationStartedAt.set(null);
    $turnStartedAt.set(null);
    $lastTurnDurationMs.set(null);
    $lastEventAt.set(null);
    $cancelRequested.set(false);
    $notifications.set({enabled: true});
    registerTranscriptScrollController(null);
    resetNotificationsForTests();
  });

  it('routes transcript scrolling through the mounted viewport controller', () => {
    const scrollBy = vi.fn();
    const scrollTo = vi.fn();
    const scrollToBottom = vi.fn();
    let listener = () => {};
    const cleanup = registerTranscriptScrollController({
      scrollBy,
      scrollTo,
      scrollToBottom,
      getScrollTop: () => 12,
      isSticky: () => false,
      subscribe: next => {
        listener = next;
        return () => {};
      }
    });

    expect($transcriptScroll.get()).toBe(12);
    expect($transcriptFollow.get()).toBe(false);

    scrollTranscriptBy(-4);
    expect(scrollBy).toHaveBeenCalledWith(-4);
    expect($transcriptFollow.get()).toBe(false);

    scrollTranscriptTo('top');
    expect(scrollTo).toHaveBeenCalledWith(0);
    scrollTranscriptTo('bottom');
    expect(scrollToBottom).toHaveBeenCalledTimes(1);
    expect($transcriptFollow.get()).toBe(true);

    listener();
    expect($transcriptScroll.get()).toBe(12);
    cleanup();
  });

  it('applies initialize state', () => {
    applyInitialize({
      project: {project_name: 'demo'},
      model: 'deepseek:chat',
      bridge: {model_timeout_seconds: 10},
      context_pressure: {stm: 12000, stm_max: 50000, wm: 11000, wm_max: 50000},
      continued_session: false,
      permissions: {allow_all: false},
      notifications: {enabled: true},
      slash_commands: [{name: '/help', help: 'Show help'}]
    });

    expect($status.get()).toBe('ready');
    expect($continuedSession.get()).toBe(false);
    expect($contextPressure.get()).toMatchObject({stm: 12000, wm: 11000});
    expect($staticLogItems.get()[0]).toMatchObject({role: 'startup'});
    expect($notifications.get()).toEqual({enabled: true});
  });

  it('records final answer events', () => {
    applyBridgeEvent({
      event_id: '1',
      turn_id: 't',
      type: 'final_answer',
      payload: {content: 'done'}
    });

    expect($transcript.get()[0]).toMatchObject({role: 'assistant', content: 'done'});
    expect($staticLogItems.get()[0]).toMatchObject({role: 'assistant', content: 'done'});
    expect($liveItems.get()).toEqual([]);
    expect($status.get()).toBe('ready');
  });

  it('records the latest response duration in the runtime state', () => {
    vi.spyOn(Date, 'now')
      .mockReturnValueOnce(1000)
      .mockReturnValueOnce(3250);

    startTurnTimer();
    finishTurnTimer();

    expect($lastTurnDurationMs.get()).toBe(2250);
    expect($turnStartedAt.get()).toBeNull();
    vi.restoreAllMocks();
  });

  it('does not notify while the terminal is focused', () => {
    const notify = vi.fn();
    setNotifierForTests({notify});
    setTerminalFocusedForTests(true);

    applyBridgeEvent({
      event_id: '1',
      turn_id: 't',
      type: 'final_answer',
      payload: {content: 'done'}
    });

    expect(notify).not.toHaveBeenCalled();
  });

  it('notifies key user-visible events while the terminal is not focused', () => {
    const notify = vi.fn();
    setNotifierForTests({notify});
    setTerminalFocusedForTests(false);

    applyBridgeEvent({
      event_id: '1',
      turn_id: 't',
      type: 'final_answer',
      payload: {message_id: 'assistant-1', content: 'A'.repeat(200)}
    });
    applyBridgeEvent({
      event_id: '2',
      turn_id: 't',
      type: 'confirmation_requested',
      payload: {title: 'write_file', details: 'notes.md'}
    });
    applyBridgeEvent({
      event_id: '3',
      turn_id: 't',
      type: 'agent_error',
      payload: {error: 'provider failed'}
    });

    expect(notify).toHaveBeenCalledTimes(3);
    expect(notify).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        title: 'Sema replied',
        message: expect.stringMatching(/^A{119}…$/)
      })
    );
    expect(notify).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        title: 'Sema needs confirmation',
        message: 'write_file - notes.md'
      })
    );
    expect(notify).toHaveBeenNthCalledWith(
      3,
      expect.objectContaining({
        title: 'Sema error',
        message: 'provider failed'
      })
    );
  });

  it('does not notify tool progress or streaming deltas', () => {
    const notify = vi.fn();
    setNotifierForTests({notify});
    setTerminalFocusedForTests(false);

    applyBridgeEvent({
      event_id: 'delta',
      turn_id: 't',
      type: 'assistant_delta',
      payload: {delta: 'hello'}
    });
    applyBridgeEvent({
      event_id: 'tool',
      turn_id: 't',
      type: 'tool_finished',
      payload: {tool: 'read_file', status: 'ok'}
    });

    expect(notify).not.toHaveBeenCalled();
  });

  it('stores context pressure telemetry without adding transcript noise', () => {
    applyBridgeEvent({
      event_id: 'pressure',
      turn_id: 't',
      type: 'context_pressure',
      payload: {
        budget_source: 'dynamic',
        stm: '11726',
        stm_max: 50000,
        wm: 10600,
        wm_max: 50000,
        I: 0.21,
        S: 0.38,
        R: 0,
        L: 0.44,
        P_context: 0.27
      }
    });

    expect($contextPressure.get()).toMatchObject({
      budget_source: 'dynamic',
      stm: 11726,
      stm_max: 50000,
      wm: 10600,
      I: 0.21,
      S: 0.38,
      R: 0,
      L: 0.44,
      P_context: 0.27
    });
    expect($transcript.get()).toEqual([]);
  });

  it('stores and responds to pending confirmations', async () => {
    const respondToConfirmation = vi.fn().mockResolvedValue({ok: true});
    const client = {respondToConfirmation} as never;
    applyBridgeEvent({
      event_id: '1',
      turn_id: 't',
      type: 'confirmation_requested',
      payload: {
        confirmation_id: 'c',
        kind: 'edit',
        title: 'write',
        details: 'path',
        choices: [],
        preview: ''
      }
    });

    await respondToPendingConfirmation(client, 'yes');

    expect(respondToConfirmation).toHaveBeenCalledWith('c', 'yes', '');
    expect($pendingConfirmation.get()).toBeNull();
    expect($status.get()).toBe('continuing');
  });

  it('tracks intent routing and model waiting states', () => {
    applyBridgeEvent({
      event_id: '0',
      turn_id: 't',
      type: 'intent_routing_started',
      payload: {}
    });

    expect($busy.get()).toBe(true);
    expect($status.get()).toBe('reading your intent');
    expect($foregroundStatus.get()).toBe('Reading your intent...');

    applyBridgeEvent({
      event_id: '0b',
      turn_id: 't',
      type: 'intent_routing_finished',
      payload: {mode: 'agent'}
    });

    expect($busy.get()).toBe(true);
    expect($status.get()).toBe('waiting for response');
    expect($foregroundStatus.get()).toBe('Reading your intent...');

    applyBridgeEvent({
      event_id: '1',
      turn_id: 't',
      type: 'model_thinking',
      payload: {}
    });

    expect($busy.get()).toBe(true);
    expect($status.get()).toBe('waiting for response');
    expect($operationStartedAt.get()).toEqual(expect.any(Number));

    applyBridgeEvent({
      event_id: '2',
      turn_id: 't',
      type: 'final_answer',
      payload: {content: 'done'}
    });

    expect($busy.get()).toBe(false);
    expect($operationStartedAt.get()).toBeNull();
    expect($foregroundStatus.get()).toBe('');
  });

  it('shows coordinator task state events as foreground status', () => {
    applyBridgeEvent({
      event_id: 'stage',
      turn_id: 't',
      type: 'assistant_stage',
      payload: {message_id: 'assistant-1', label: 'Reading your intent...'}
    });

    applyBridgeEvent({
      event_id: 'coord-start',
      turn_id: 't',
      type: 'coordinator_state_updating',
      payload: {action: 'create'}
    });

    expect($liveItems.get()).toEqual([]);
    expect($foregroundStatus.get()).toBe('Planning the task...');
    expect($staticLogItems.get()).toEqual([]);

    applyBridgeEvent({
      event_id: 'coord-finish',
      turn_id: 't',
      type: 'coordinator_state_updated',
      payload: {action: 'create'}
    });

    expect($liveItems.get()).toEqual([]);
    expect($foregroundStatus.get()).toBe('');
    expect($staticLogItems.get()).toEqual([]);
  });

  it('shows reviewer status without committing transcript noise before final answer', () => {
    applyBridgeEvent({
      event_id: 'review-start',
      turn_id: 't',
      type: 'reviewer_started',
      payload: {}
    });

    expect($liveItems.get()).toEqual([]);
    expect($foregroundStatus.get()).toBe('Reviewing the final answer...');
    expect($staticLogItems.get()).toEqual([]);

    applyBridgeEvent({
      event_id: 'review-finish',
      turn_id: 't',
      type: 'reviewer_finished',
      payload: {passed: true, recommended_next_action: 'final', concerns_count: 0}
    });

    expect($liveItems.get()).toEqual([]);
    expect($foregroundStatus.get()).toBe('');
    expect($staticLogItems.get()).toEqual([]);

    applyBridgeEvent({
      event_id: 'final',
      turn_id: 't',
      type: 'final_answer',
      payload: {content: 'Reviewed answer.'}
    });

    expect($staticLogItems.get()).toHaveLength(1);
    expect($staticLogItems.get()[0]).toMatchObject({role: 'assistant', content: 'Reviewed answer.'});
    expect($liveItems.get()).toEqual([]);
  });

  it('ignores unknown background events', () => {
    applyBridgeEvent({
      event_id: 'unknown-start',
      turn_id: 't',
      type: 'unknown_background_started',
      payload: {message_count: 10}
    });

    expect($busy.get()).toBe(false);
    expect($backgroundActivities.get()).toEqual([]);
    expect($liveItems.get()).toEqual([]);
    expect($staticLogItems.get()).toEqual([]);

    applyBridgeEvent({
      event_id: 'unknown-finish',
      turn_id: 't',
      type: 'unknown_background_finished',
      payload: {status: 'ok', created_count: 1, added_refs: 2}
    });

    expect($busy.get()).toBe(false);
    expect($backgroundActivities.get()).toEqual([]);
    expect($liveItems.get()).toEqual([]);
    expect($staticLogItems.get()).toEqual([]);
  });

  it('tracks WM and focus lifecycle processes in the shared process row', () => {
    const processes = [
      ['focus_detector_started', 'focus-detector', 'Focus Detector is working'],
      ['working_memory_preflight_started', 'working-memory-preflight', 'WM Preflight Recovery is working'],
      ['working_memory_maintenance_started', 'working-memory-maintainer', 'Working Memory Maintainer is working'],
      ['segment_ingest_started', 'focus-ingester', 'Focus Ingester is running'],
      ['full_segment_maintenance_started', 'focus-maintainer', 'Focus Maintainer is running'],
      ['memory_push_started', 'memory-push', 'LTM Push is running'],
      ['ltm_formation_started', 'ltm-formation', 'LTM Formationer is running'],
      ['formation_backfill_started', 'formation-backfill', 'LTM Formationer (Backfill) is running'],
      ['memorize_started', 'ltm-memorize', 'LTM Memorizer is running'],
      ['ltm_maintenance_started', 'ltm-maintainer', 'LTM Maintainer is running']
    ] as const;

    for (const [type, id, label] of processes) {
      applyBridgeEvent({event_id: `${id}-start`, turn_id: 't', type, payload: {}});
      expect($backgroundActivities.get()).toContainEqual(expect.objectContaining({id, kind: 'background', label}));
    }
    expect($liveItems.get()).toEqual([]);
    expect($staticLogItems.get()).toEqual([]);

    for (const [, id] of processes) {
      const typeById: Record<string, string> = {
        'focus-detector': 'focus_detector_finished',
        'working-memory-preflight': 'working_memory_preflight_finished',
        'working-memory-maintainer': 'working_memory_maintenance_finished',
        'focus-ingester': 'segment_ingest_finished',
        'focus-maintainer': 'full_segment_maintenance_finished',
        'memory-push': 'memory_push_finished',
        'ltm-formation': 'ltm_formation_finished',
        'formation-backfill': 'formation_backfill_finished',
        'ltm-memorize': 'memorize_finished',
        'ltm-maintainer': 'ltm_maintenance_completed'
      };
      const type = typeById[id]!;
      applyBridgeEvent({event_id: `${id}-finish`, turn_id: 't', type, payload: {status: 'ok'}});
    }
    expect($backgroundActivities.get()).toEqual([]);
  });

  it('records last backend event time for idle display', () => {
    applyBridgeEvent({
      event_id: '1',
      turn_id: 't',
      type: 'model_thinking',
      payload: {}
    });

    expect($lastEventAt.get()).toEqual(expect.any(Number));
  });

  it('updates inline tool transcript from running to finished', () => {
    applyBridgeEvent({
      event_id: 'start',
      turn_id: 'turn',
      type: 'tool_started',
      payload: {tool: 'read_file', args: {path: 'a.py'}}
    });

    expect($tools.get()[0]).toMatchObject({status: 'running', tool: 'read_file'});
    expect($liveItems.get()[0]).toMatchObject({role: 'tool', content: 'running Read File'});

    applyBridgeEvent({
      event_id: 'finish',
      turn_id: 'turn',
      type: 'tool_finished',
      payload: {tool: 'read_file', args: {path: 'a.py'}, status: 'ok', result: 'content'}
    });

    expect($tools.get()).toHaveLength(1);
    expect($tools.get()[0]).toMatchObject({status: 'ok', result: 'content'});
    expect($liveItems.get()).toEqual([]);
    expect($transcript.get()[0]).toMatchObject({role: 'tool', content: 'ok Read File'});
    expect($transcript.get()[0].tool).toMatchObject({status: 'ok', result: 'content'});
  });

  it('opens latest tool detail output panel', () => {
    applyBridgeEvent({
      event_id: 'start',
      turn_id: 'turn',
      type: 'tool_started',
      payload: {tool: 'web_search', args: {query: 'f1'}}
    });
    applyBridgeEvent({
      event_id: 'finish',
      turn_id: 'turn',
      type: 'tool_finished',
      payload: {tool: 'web_search', args: {query: 'f1'}, status: 'ok', result: 'result text'}
    });

    openLatestToolDetail();

    expect($activeView.get()).toBe('commandOutput');
    expect($commandOutput.get()?.title).toBe('Tool: Web Search');
    expect($commandOutput.get()?.content).toContain('"query": "f1"');
    expect($commandOutput.get()?.content).toContain('result text');
  });

  it('merges streaming assistant deltas and final content by message id', () => {
    applyBridgeEvent({
      event_id: 'stage',
      turn_id: 'turn',
      type: 'assistant_stage',
      payload: {message_id: 'assistant-1', label: 'Reading your intent...'}
    });

    expect($foregroundStatus.get()).toBe('Reading your intent...');
    expect($transcript.get()).toHaveLength(0);
    expect($liveItems.get()).toHaveLength(0);

    applyBridgeEvent({
      event_id: 'delta-1',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: 'Hel'}
    });
    applyBridgeEvent({
      event_id: 'delta-2',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: 'lo'}
    });

    expect($transcript.get()).toHaveLength(1);
    expect($liveItems.get()).toHaveLength(1);
    expect($liveItems.get()[0]).toMatchObject({id: 'assistant-1', content: 'Hello'});
    expect($foregroundStatus.get()).toBe('Responding...');
    expect($streamingAssistantBuffers.get()).toMatchObject({'assistant-1': 'Hello'});

    applyBridgeEvent({
      event_id: 'final',
      turn_id: 'turn',
      type: 'final_answer',
      payload: {message_id: 'assistant-1', content: 'Hello.'}
    });

    expect($transcript.get()).toHaveLength(1);
    expect($staticLogItems.get()[0]).toMatchObject({id: 'assistant-1', role: 'assistant', content: 'Hello.', markdown: true});
    expect($liveItems.get()).toEqual([]);
    expect($transcript.get()[0]).toMatchObject({id: 'assistant-1', role: 'assistant', content: 'Hello.', markdown: true});
    expect($streamingAssistantBuffers.get()).toEqual({});
    expect($busy.get()).toBe(false);
  });

  it('keeps static and live items in event order when they overlap', () => {
    applyBridgeEvent({
      event_id: 'delta',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: 'Before the tool'}
    });

    appendTranscript('system', 'A later static event');

    expect($liveItems.get()).toHaveLength(1);
    expect($staticLogItems.get()).toHaveLength(1);
    expect($transcript.get().map(item => item.content)).toEqual(['Before the tool', 'A later static event']);
  });

  it('preserves repeated streaming text instead of guessing that it is a duplicate', () => {
    applyBridgeEvent({
      event_id: 'delta-1',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: '你说得对，我完全同意这个方向。'}
    });
    applyBridgeEvent({
      event_id: 'delta-2',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: '你说得对，我完全同意这个方向。'}
    });

    expect($transcript.get()).toHaveLength(1);
    expect($liveItems.get()).toHaveLength(1);
    expect($streamingAssistantBuffers.get()).toMatchObject({'assistant-1': '你说得对，我完全同意这个方向。你说得对，我完全同意这个方向。'});
  });

  it('appends numeric streaming deltas without dropping short chunks', () => {
    applyBridgeEvent({
      event_id: 'delta-1',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: '1\n2\n3\n'}
    });
    applyBridgeEvent({
      event_id: 'delta-2',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: '4\n5\n6\n'}
    });

    expect($transcript.get()).toHaveLength(1);
    expect($liveItems.get()).toHaveLength(1);
    expect($streamingAssistantBuffers.get()).toMatchObject({'assistant-1': '1\n2\n3\n4\n5\n6\n'});
  });

  it('updates an existing assistant transcript item when final content repeats for the same message id', () => {
    applyBridgeEvent({
      event_id: 'delta',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: 'Draft'}
    });
    applyBridgeEvent({
      event_id: 'content',
      turn_id: 'turn',
      type: 'assistant_content',
      payload: {message_id: 'assistant-1', content: 'Final answer.'}
    });
    applyBridgeEvent({
      event_id: 'final',
      turn_id: 'turn',
      type: 'final_answer',
      payload: {message_id: 'assistant-1', content: 'Final answer.'}
    });

    expect($transcript.get()).toHaveLength(1);
    expect($staticLogItems.get()).toHaveLength(1);
    expect($transcript.get()[0]).toMatchObject({id: 'assistant-1', role: 'assistant', content: 'Final answer.', markdown: true});
    expect($liveItems.get()).toEqual([]);
  });

  it('removes assistant stage placeholders but keeps streamed text when a tool starts', () => {
    applyBridgeEvent({
      event_id: 'stage',
      turn_id: 'turn',
      type: 'assistant_stage',
      payload: {message_id: 'assistant-1', label: 'Waiting for the model...'}
    });
    applyBridgeEvent({
      event_id: 'delta',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: 'I will inspect it.'}
    });

    applyBridgeEvent({
      event_id: 'tool-start',
      turn_id: 'turn',
      type: 'tool_started',
      payload: {tool: 'read_file', args: {path: 'a.py'}}
    });

    expect($transcript.get()).toHaveLength(2);
    expect($staticLogItems.get()[0]).toMatchObject({role: 'assistant', content: 'I will inspect it.'});
    expect($liveItems.get()[0]).toMatchObject({role: 'tool'});
    expect($transcript.get()[0]).toMatchObject({role: 'assistant', content: 'I will inspect it.'});
    expect($transcript.get()[1]).toMatchObject({role: 'tool'});
    expect($streamingAssistantBuffers.get()).toEqual({});
  });

  it('replays a sequenced turn into one ordered transcript without duplicate items', () => {
    const events = [
      {event_id: 'user-1', turn_id: 'turn-1', sequence: 1, type: 'user_message', payload: {content: 'inspect'}},
      {event_id: 'delta-1', turn_id: 'turn-1', sequence: 2, type: 'assistant_delta', payload: {message_id: 'assistant-1', delta: 'I will inspect it.'}},
      {event_id: 'tool-start-1', turn_id: 'turn-1', sequence: 3, type: 'tool_started', payload: {tool: 'read_file', item_id: 'tool-1', args: {path: 'a.py'}}},
      {event_id: 'tool-finish-1', turn_id: 'turn-1', sequence: 4, type: 'tool_finished', payload: {tool: 'read_file', item_id: 'tool-1', status: 'ok', result: 'content'}},
      {event_id: 'delta-2', turn_id: 'turn-1', sequence: 5, type: 'assistant_delta', payload: {message_id: 'assistant-1', delta: 'The file is valid.'}},
      {event_id: 'final-1', turn_id: 'turn-1', sequence: 6, type: 'final_answer', payload: {message_id: 'assistant-1', content: 'The file is valid.'}}
    ] as const;

    for (const event of events) {
      applyBridgeEvent(event);
    }

    expect($transcript.get().map(item => item.content)).toEqual([
      'inspect',
      'I will inspect it.',
      'ok Read File',
      'The file is valid.'
    ]);
    expect($transcript.get().map(item => item.sequence)).toEqual([1, 2, 3, 5]);
    expect(new Set($transcript.get().map(item => item.id)).size).toBe($transcript.get().length);
    expect($liveItems.get()).toEqual([]);
    expect($staticLogItems.get().filter(item => item.role !== 'startup')).toHaveLength(4);
  });

  it('keeps user and assistant entries alternating across turns', () => {
    applyBridgeEvent({
      event_id: 'user-1',
      turn_id: 'turn-1',
      sequence: 1,
      type: 'user_message',
      payload: {content: 'first'}
    });
    applyBridgeEvent({
      event_id: 'answer-1',
      turn_id: 'turn-1',
      sequence: 2,
      type: 'final_answer',
      payload: {message_id: 'assistant-1', content: 'first answer'}
    });
    applyBridgeEvent({
      event_id: 'user-2',
      turn_id: 'turn-2',
      sequence: 3,
      type: 'user_message',
      payload: {content: 'second'}
    });
    applyBridgeEvent({
      event_id: 'answer-2',
      turn_id: 'turn-2',
      sequence: 4,
      type: 'final_answer',
      payload: {message_id: 'assistant-2', content: 'second answer'}
    });

    expect($transcript.get().map(item => `${item.role}:${item.content}`)).toEqual([
      'user:first',
      'assistant:first answer',
      'user:second',
      'assistant:second answer'
    ]);
  });

  it('aggregates edit stats for the current turn and clears on next user message', () => {
    applyBridgeEvent({
      event_id: 'finish-1',
      turn_id: 'turn',
      type: 'tool_finished',
      payload: {tool: 'write_file', args: {path: 'a.py'}, status: 'ok', result: 'Wrote file: a.py', edit_stats: {added: 10, removed: 2, files: ['a.py']}}
    });
    applyBridgeEvent({
      event_id: 'finish-2',
      turn_id: 'turn',
      type: 'tool_finished',
      payload: {tool: 'replace_block', args: {path: 'b.py'}, status: 'ok', result: 'Replaced block in: b.py', edit_stats: {added: 5, removed: 7, files: ['b.py']}}
    });

    expect($turnCodeChanges.get()).toEqual({added: 15, removed: 9, files: ['a.py', 'b.py']});

    applyBridgeEvent({
      event_id: 'user',
      turn_id: 'turn-2',
      type: 'user_message',
      payload: {content: 'next'}
    });

    expect($turnCodeChanges.get()).toEqual({added: 0, removed: 0, files: []});
  });

  it('removes pure assistant stage placeholders when a tool starts', () => {
    applyBridgeEvent({
      event_id: 'stage',
      turn_id: 'turn',
      type: 'assistant_stage',
      payload: {message_id: 'assistant-1', label: 'Waiting for the model...'}
    });

    applyBridgeEvent({
      event_id: 'tool-start',
      turn_id: 'turn',
      type: 'tool_started',
      payload: {tool: 'read_file', args: {path: 'a.py'}}
    });

    expect($transcript.get()).toHaveLength(1);
    expect($liveItems.get()[0]).toMatchObject({role: 'tool'});
    expect($transcript.get()[0]).toMatchObject({role: 'tool'});
  });

  it('counts final assistant output after hidden streaming deltas as visible turn output', async () => {
    let resolveSend: (value: unknown) => void = () => {};
    const sendMessage = vi.fn().mockReturnValue(new Promise(resolve => {
      resolveSend = resolve;
    }));
    const client = {sendMessage} as never;

    const submitted = submitPrompt(client, 'hello');

    applyBridgeEvent({
      event_id: 'delta',
      turn_id: 'turn',
      type: 'assistant_delta',
      payload: {message_id: 'assistant-1', delta: 'Hi'}
    });
    applyBridgeEvent({
      event_id: 'final',
      turn_id: 'turn',
      type: 'final_answer',
      payload: {message_id: 'assistant-1', content: 'Hi'}
    });

    resolveSend({ok: true});
    await submitted;

    expect($transcript.get()).toHaveLength(2);
    expect($transcript.get()[0]).toMatchObject({role: 'user', content: 'hello'});
    expect($transcript.get()[1]).toMatchObject({role: 'assistant', content: 'Hi'});
  });

  it('finishes activity when the backend stops a tool run', () => {
    applyBridgeEvent({
      event_id: 'stop',
      turn_id: 'turn',
      type: 'agent_stopped',
      payload: {message: 'Tool run stopped by user.'}
    });

    expect($busy.get()).toBe(false);
    expect($status.get()).toBe('ready');
    expect($transcript.get()[0]).toMatchObject({role: 'assistant', content: 'Tool run stopped by user.'});
  });

  it('tracks cancellation request and cancelled event', async () => {
    const cancel = vi.fn().mockResolvedValue({ok: true});
    const client = {cancelTurn: cancel} as never;

    await cancelTurn(client);

    expect(cancel).toHaveBeenCalled();
    expect($cancelRequested.get()).toBe(true);
    expect($status.get()).toBe('cancelling');

    applyBridgeEvent({
      event_id: 'cancelled',
      turn_id: 'turn',
      type: 'turn_cancelled',
      payload: {message: 'Turn cancelled by user.'}
    });

    expect($cancelRequested.get()).toBe(false);
    expect($busy.get()).toBe(false);
  });

  it('submits slash commands through store action', async () => {
    const executeSlash = vi.fn().mockResolvedValue({title: 'Status', content: 'ok', should_exit: false});
    const client = {executeSlash} as never;

    await submitPrompt(client, '/status');

    expect(executeSlash).toHaveBeenCalledWith('/status');
    expect($commandOutput.get()).toMatchObject({title: 'Status', content: 'ok'});
    expect($activeView.get()).toBe('commandOutput');
    expect($status.get()).toBe('ready');
  });

  it('opens structured last-run output from store action', async () => {
    const executeSlash = vi.fn().mockResolvedValue({
      title: 'Last run',
      content: '1. read_file\nargs: {"path":"a.py"}\ncontent',
      should_exit: false
    });
    const client = {executeSlash} as never;

    await submitPrompt(client, '/last-run');

    expect(executeSlash).toHaveBeenCalledWith('/last-run');
    expect($commandOutput.get()?.title).toBe('Last run');
    expect($commandOutput.get()?.content).toContain('read_file');
    expect($activeView.get()).toBe('commandOutput');
  });

  it('opens notify output and syncs notification config from store action', async () => {
    const executeSlash = vi.fn().mockResolvedValue({
      title: 'Notify',
      content: 'Sema notifications:\nenabled: false',
      notifications: {enabled: false}
    });
    const client = {executeSlash} as never;

    await submitPrompt(client, '/notify off');

    expect(executeSlash).toHaveBeenCalledWith('/notify off');
    expect($notifications.get()).toEqual({enabled: false});
    expect($commandOutput.get()?.title).toBe('Notify');
    expect($commandOutput.get()?.content).toContain('focus_tracking_runtime: unknown');
  });

  it('formats reviewer details in structured trace output', async () => {
    const executeSlash = vi.fn().mockResolvedValue({
      title: 'Trace current',
      content: '10:52:26 reviewer ok pass=false next=continue concerns=2 source=state_consistency',
      should_exit: false
    });
    const client = {executeSlash} as never;

    await submitPrompt(client, '/trajectory current');

    expect(executeSlash).toHaveBeenCalledWith('/trajectory current');
    expect($commandOutput.get()?.title).toBe('Trace current');
    expect($commandOutput.get()?.content).toContain(
      '10:52:26 reviewer ok pass=false next=continue concerns=2 source=state_consistency'
    );
  });

  it('closes transient output panels before sending a normal prompt', async () => {
    const sendMessage = vi.fn().mockResolvedValue({ok: true});
    const client = {sendMessage} as never;
    $commandOutput.set({title: 'Trace', content: 'event'});
    $activeView.set('commandOutput');

    await submitPrompt(client, 'hello');

    expect(sendMessage).toHaveBeenCalledWith('hello');
    expect($commandOutput.get()).toBeNull();
    expect($activeView.get()).toBe('chat');
  });

  it('keeps submitted prompts pending until visible backend output arrives', async () => {
    let resolveSend: (value: unknown) => void = () => {};
    const sendMessage = vi.fn().mockReturnValue(new Promise(resolve => {
      resolveSend = resolve;
    }));
    const client = {sendMessage} as never;

    const submitted = submitPrompt(client, 'hello');

    expect($pendingUserMessage.get()).toMatchObject({content: 'hello'});
    expect($transcript.get()).toHaveLength(1);
    expect($transcript.get()[0]).toMatchObject({role: 'user', content: 'hello'});
    expect($transcript.get()[0].provisional).toBeUndefined();

    applyBridgeEvent({
      event_id: 'backend-user',
      turn_id: 'turn',
      type: 'user_message',
      payload: {content: 'hello'}
    });

    expect($pendingUserMessage.get()).toMatchObject({content: 'hello'});
    expect($transcript.get()).toHaveLength(1);
    expect($transcript.get()[0]).toMatchObject({role: 'user', content: 'hello'});
    expect($transcript.get()[0].provisional).toBeUndefined();

    applyBridgeEvent({
      event_id: 'tool-start',
      turn_id: 'turn',
      type: 'tool_started',
      payload: {tool: 'read_file', args: {path: 'a.py'}}
    });

    expect($pendingUserMessage.get()).toBeNull();
    expect($transcript.get()[0]).toMatchObject({role: 'user', content: 'hello'});
    expect($transcript.get()[1]).toMatchObject({role: 'tool'});
    resolveSend({ok: true});
    await submitted;
  });

  it('adds a visible notice when a normal prompt completes without sema output', async () => {
    const sendMessage = vi.fn().mockResolvedValue({ok: true});
    const client = {sendMessage} as never;

    await submitPrompt(client, 'hello');

    expect($transcript.get()).toHaveLength(2);
    expect($transcript.get()[0]).toMatchObject({role: 'user', content: 'hello'});
    expect($transcript.get()[1]).toMatchObject({role: 'assistant', content: 'Sema finished this turn without visible output.'});
  });

  it('records backend user events when there is no optimistic echo', () => {
    applyBridgeEvent({
      event_id: 'backend-user',
      turn_id: 'turn',
      type: 'user_message',
      payload: {content: 'hello'}
    });

    expect($transcript.get()).toHaveLength(1);
    expect($transcript.get()[0]).toMatchObject({role: 'user', content: 'hello'});
  });

  it('loads config through store action', async () => {
    const getConfig = vi.fn().mockResolvedValue({config: {provider: 'deepseek'}, profiles: {}, path: 'config.json'});
    const client = {getConfig} as never;

    await submitPrompt(client, '/config');

    expect($config.get()?.config).toEqual({provider: 'deepseek'});
    expect($status.get()).toBe('ready');
  });
});
