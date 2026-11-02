import {maybeNotifyBridgeEvent} from '../notifications.js';
import type {BridgeEvent, ConfirmationRequest, ContextPressureTelemetry, ToolRecord} from '../protocol/types.js';
import {
  $activeView, $cancelRequested, $contextPressure, $lastError, $lastEventAt,
  $pendingConfirmation, $streamingAssistantBuffers, $turnCodeChanges, $turnStatus
} from './runtime_state.js';

export interface RuntimeEventReducerDependencies {
  setForegroundStatus: (status: string) => void;
  setBusy: (label: string, resetStartedAt?: boolean) => void;
  startTurnTimer: () => void;
  finishTurnTimer: () => void;
  finishActivity: (label?: string) => void;
  applyUserMessageEvent: (content: string, eventId: string, turnId: string, sequence: number) => void;
  materializePendingUserMessage: () => void;
  removeProvisionalAssistantMessages: () => void;
  removeLiveItem: (id: string) => void;
  coordinatorStatusLabel: (action: unknown) => string;
  normalizeContextPressure: (payload: Record<string, unknown>) => ContextPressureTelemetry;
  upsertBackgroundActivity: (id: string, kind: 'background', label: string) => void;
  removeBackgroundActivity: (id: string) => void;
  streamMessageId: (event: BridgeEvent<Record<string, unknown>>) => string;
  upsertAssistantDelta: (messageId: string, delta: string, sequence: number) => void;
  upsertAssistantContent: (messageId: string, content: string, fallbackId: string, sequence: number) => void;
  commitProvisionalAssistantText: () => void;
  upsertTool: (record: ToolRecord) => void;
  finishTool: (event: BridgeEvent<Record<string, unknown>>) => void;
  appendTranscript: (role: 'user' | 'assistant' | 'tool' | 'system', content: string, id?: string) => void;
  failRequest: (error: string) => void;
}

export function reduceBridgeEvent(
  event: BridgeEvent<Record<string, unknown>>,
  deps: RuntimeEventReducerDependencies
) {
  $lastEventAt.set(Date.now());
  maybeNotifyBridgeEvent(event);
  switch (event.type) {
    case 'user_message':
      deps.startTurnTimer();
      $turnCodeChanges.set({added: 0, removed: 0, files: []});
      deps.setForegroundStatus('');
      $streamingAssistantBuffers.set({});
      deps.applyUserMessageEvent(String(event.payload.content ?? ''), event.event_id, event.turn_id, eventSequence(event));
      $turnStatus.set('running');
      deps.setBusy('waiting for response');
      break;
    case 'intent_routing_started':
      deps.setForegroundStatus('Reading your intent...');
      deps.setBusy('reading your intent');
      break;
    case 'intent_routing_finished':
      deps.setBusy('waiting for response');
      break;
    case 'coordinator_state_updating':
      deps.materializePendingUserMessage();
      deps.removeProvisionalAssistantMessages();
      deps.removeLiveItem('coordinator-status');
      deps.setForegroundStatus(deps.coordinatorStatusLabel(event.payload.action));
      deps.setBusy('updating task state');
      break;
    case 'coordinator_state_updated':
      deps.removeLiveItem('coordinator-status');
      deps.setForegroundStatus('');
      deps.setBusy('waiting for response');
      break;
    case 'reviewer_started':
      deps.materializePendingUserMessage();
      deps.removeProvisionalAssistantMessages();
      deps.removeLiveItem('reviewer-status');
      deps.setForegroundStatus('Reviewing the final answer...');
      deps.setBusy('reviewing final answer');
      break;
    case 'reviewer_finished':
      deps.removeLiveItem('reviewer-status');
      deps.setForegroundStatus('');
      deps.setBusy('waiting for response');
      break;
    case 'context_pressure':
      $contextPressure.set(deps.normalizeContextPressure(event.payload));
      break;
    case 'working_memory_preflight_started':
      deps.upsertBackgroundActivity('working-memory-preflight', 'background', 'WM Preflight Recovery is working');
      break;
    case 'working_memory_preflight_finished':
      deps.removeBackgroundActivity('working-memory-preflight');
      break;
    case 'focus_detector_started':
      deps.upsertBackgroundActivity('focus-detector', 'background', 'Focus Detector is working');
      break;
    case 'focus_detector_finished':
      deps.removeBackgroundActivity('focus-detector');
      break;
    case 'working_memory_maintenance_started':
      deps.upsertBackgroundActivity('working-memory-maintainer', 'background', 'Working Memory Maintainer is working');
      break;
    case 'working_memory_maintenance_finished':
      deps.removeBackgroundActivity('working-memory-maintainer');
      break;
    case 'segment_ingest_started':
      deps.upsertBackgroundActivity('focus-ingester', 'background', 'Focus Ingester is running');
      break;
    case 'segment_ingest_finished':
      deps.removeBackgroundActivity('focus-ingester');
      break;
    case 'full_segment_maintenance_started':
      deps.upsertBackgroundActivity('focus-maintainer', 'background', 'Focus Maintainer is running');
      break;
    case 'full_segment_maintenance_finished':
      deps.removeBackgroundActivity('focus-maintainer');
      break;
    case 'memory_push_started':
      deps.upsertBackgroundActivity('memory-push', 'background', 'LTM Push is running');
      break;
    case 'memory_push_finished':
      deps.removeBackgroundActivity('memory-push');
      break;
    case 'ltm_formation_started':
      deps.upsertBackgroundActivity('ltm-formation', 'background', 'LTM Formationer is running');
      break;
    case 'ltm_formation_finished':
      deps.removeBackgroundActivity('ltm-formation');
      break;
    case 'formation_backfill_started':
      deps.upsertBackgroundActivity('formation-backfill', 'background', 'LTM Formationer (Backfill) is running');
      break;
    case 'formation_backfill_finished':
      deps.removeBackgroundActivity('formation-backfill');
      break;
    case 'memorize_started':
      deps.upsertBackgroundActivity('ltm-memorize', 'background', 'LTM Memorizer is running');
      break;
    case 'memorize_finished':
      deps.removeBackgroundActivity('ltm-memorize');
      break;
    case 'ltm_maintenance_started':
      deps.upsertBackgroundActivity('ltm-maintainer', 'background', 'LTM Maintainer is running');
      break;
    case 'ltm_maintenance_completed':
    case 'ltm_maintenance_failed':
      deps.removeBackgroundActivity('ltm-maintainer');
      break;
    case 'assistant_stage':
      deps.materializePendingUserMessage();
      deps.setForegroundStatus(String(event.payload.label ?? 'Working...'));
      deps.setBusy('waiting for response');
      break;
    case 'assistant_delta':
      deps.materializePendingUserMessage();
      deps.removeLiveItem('coordinator-status');
      deps.setForegroundStatus('Responding...');
      deps.upsertAssistantDelta(deps.streamMessageId(event), String(event.payload.delta ?? ''), eventSequence(event));
      deps.setBusy('receiving response');
      break;
    case 'assistant_content':
      deps.materializePendingUserMessage();
      deps.removeLiveItem('coordinator-status');
      deps.setForegroundStatus('');
      deps.upsertAssistantContent(deps.streamMessageId(event), String(event.payload.content ?? ''), event.event_id, eventSequence(event));
      break;
    case 'final_answer':
      deps.materializePendingUserMessage();
      deps.removeLiveItem('coordinator-status');
      deps.removeLiveItem('reviewer-status');
      deps.setForegroundStatus('');
      deps.upsertAssistantContent(deps.streamMessageId(event), String(event.payload.content ?? ''), event.event_id, eventSequence(event));
      deps.finishTurnTimer();
      deps.finishActivity('ready');
      break;
    case 'model_thinking':
      deps.setBusy('waiting for response');
      break;
    case 'model_error':
      $lastError.set(String(event.payload.error ?? 'Model request failed'));
      deps.setBusy(`model retry ${Number(event.payload.retry ?? 0) + 1} failed`);
      break;
    case 'tool_started':
      deps.materializePendingUserMessage();
      deps.removeLiveItem('coordinator-status');
      deps.setForegroundStatus('');
      deps.commitProvisionalAssistantText();
      deps.removeProvisionalAssistantMessages();
      deps.upsertTool({
        id: String(event.payload.item_id || event.event_id),
        turn_id: event.turn_id,
        tool: String(event.payload.tool ?? 'tool'),
        args: event.payload.args,
        status: 'running',
        result: '',
        sequence: eventSequence(event)
      });
      deps.setBusy(`running ${String(event.payload.tool ?? 'tool')}`);
      break;
    case 'tool_finished':
      deps.finishTool(event);
      break;
    case 'confirmation_requested':
      deps.materializePendingUserMessage();
      deps.setForegroundStatus('');
      $pendingConfirmation.set(event.payload as unknown as ConfirmationRequest);
      $activeView.set('confirm');
      deps.setBusy('waiting for confirmation');
      break;
    case 'reviewer_feedback':
      deps.removeLiveItem('reviewer-status');
      deps.setBusy('waiting for response');
      break;
    case 'cancel_requested':
      $cancelRequested.set(true);
      deps.setBusy('cancelling');
      break;
    case 'turn_cancelled':
      deps.materializePendingUserMessage();
      deps.setForegroundStatus('');
      deps.appendTranscript('assistant', String(event.payload.message ?? 'Turn cancelled by user.'), event.event_id);
      $cancelRequested.set(false);
      deps.finishTurnTimer();
      deps.finishActivity('ready');
      break;
    case 'agent_stopped':
      deps.materializePendingUserMessage();
      deps.setForegroundStatus('');
      deps.appendTranscript('assistant', String(event.payload.message ?? 'Tool run stopped.'), event.event_id);
      deps.finishTurnTimer();
      deps.finishActivity('ready');
      break;
    case 'agent_error':
    case 'error':
      deps.materializePendingUserMessage();
      deps.failRequest(String(event.payload.error ?? event.payload.message ?? 'Unknown error'));
      break;
  }
}

function eventSequence(event: BridgeEvent<Record<string, unknown>>) {
  const sequence = Number(event.sequence);
  return Number.isFinite(sequence) && sequence > 0 ? sequence : 0;
}
