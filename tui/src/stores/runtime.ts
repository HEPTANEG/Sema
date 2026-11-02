import {applyNotificationSettings, notifySettingsResult} from '../notifications.js';
import {$configReturnView} from './runtime_state.js';
import type {BridgeClient} from '../protocol/client.js';
import type {BridgeEvent, ContextPressureTelemetry, EditStats, InitializeResult, SlashCommand, ToolRecord} from '../protocol/types.js';
import {
  $activeView, $backgroundActivities, $bridge, $busy, $cancelRequested, $commandOutput, $config,
  $contextPressure, $continuedSession, $foregroundStatus, $initialized, $lastError, $lastEventAt,
  $liveItems, $model, $notifications, $operationStartedAt, $panelScroll, $pendingConfirmation, $settings,
  $pendingUserMessage, $permissions, $project, $slashCommands, $staticLogItems, $status, $streamingAssistantBuffers,
  $terminalColumns, $tools, $transcript, $transcriptContentHeight, $transcriptFollow,
  $transcriptViewportHeight,
  $transcriptScroll, $turnCodeChanges, $turnStartedAt, $lastTurnDurationMs, $turnStatus,
  type BackgroundActivity, type TranscriptItem
} from './runtime_state.js';
import {
  formatToolDetail, formatToolInline, formatToolName, withLocalNotifyStatus
} from './runtime_formatting.js';
import {reduceBridgeEvent} from './runtime_event_reducer.js';

export * from './runtime_state.js';

let nextTranscriptSequence = 0;
let nextArrivalSequence = 0;
type TranscriptScrollController = {
  scrollBy: (delta: number) => void;
  scrollTo: (position: number) => void;
  scrollToBottom: () => void;
  getScrollTop: () => number;
  isSticky: () => boolean;
  subscribe: (listener: () => void) => () => void;
};
let transcriptScrollController: TranscriptScrollController | null = null;

function setForegroundStatus(status: string) {
  if ($foregroundStatus.get() === status) {
    return;
  }
  $foregroundStatus.set(status);
}

function ensureStartupLogItem() {
  if ($staticLogItems.get().some(item => item.role === 'startup')) {
    return;
  }
  $staticLogItems.set([...$staticLogItems.get(), {id: 'startup', role: 'startup'}]);
}

function resetLogToStartup() {
  nextTranscriptSequence = 0;
  nextArrivalSequence = 0;
  $turnStartedAt.set(null);
  $lastTurnDurationMs.set(null);
  setTranscriptItems([]);
  $streamingAssistantBuffers.set({});
  $staticLogItems.set([{id: 'startup', role: 'startup'}]);
  $tools.set([]);
  setForegroundStatus('');
  $turnCodeChanges.set({added: 0, removed: 0, files: []});
  $contextPressure.set(null);
  $commandOutput.set(null);
  $config.set(null);
  $settings.set(null);
  $panelScroll.set(0);
  $activeView.set('chat');
  $transcriptScroll.set(0);
  $transcriptContentHeight.set(0);
  $transcriptViewportHeight.set(1);
  $transcriptFollow.set(true);
}

function setTranscriptItems(items: TranscriptItem[]) {
  const sorted = [...items].sort((left, right) => (left.sequence ?? Number.MAX_SAFE_INTEGER) - (right.sequence ?? Number.MAX_SAFE_INTEGER));
  $transcript.set(sorted);


  const startup = $staticLogItems.get().filter(item => item.role === 'startup');
  $staticLogItems.set([...startup, ...sorted.filter(item => item.state === 'complete' || (!item.state && !item.provisional))]);
  $liveItems.set(sorted.filter(item => item.state === 'streaming' || item.state === 'running' || Boolean(item.provisional)));
}

function eventSequence(event?: BridgeEvent<Record<string, unknown>>) {
  const sequence = Number(event?.sequence);
  if (Number.isFinite(sequence) && sequence > 0) {
    nextArrivalSequence = Math.max(nextArrivalSequence, sequence);
    return sequence;
  }
  nextArrivalSequence += 1;
  return nextArrivalSequence;
}

function nextLocalSequence() {
  nextTranscriptSequence = Math.max(nextTranscriptSequence, nextArrivalSequence);
  return ++nextTranscriptSequence;
}

export function applyInitialize(result: InitializeResult) {
  $status.set('ready');
  $busy.set(false);
  $operationStartedAt.set(null);
  $turnStartedAt.set(null);
  $lastTurnDurationMs.set(null);
  $lastEventAt.set(null);
  $turnStatus.set('idle');
  $cancelRequested.set(false);
  $backgroundActivities.set([]);
  setForegroundStatus('');
  $streamingAssistantBuffers.set({});
  $turnCodeChanges.set({added: 0, removed: 0, files: []});
  $contextPressure.set(null);
  $activeView.set('chat');
  $config.set(null);
  $settings.set(null);
  $transcriptScroll.set(0);
  $transcriptContentHeight.set(0);
  $transcriptViewportHeight.set(1);
  $transcriptFollow.set(true);
  $model.set(result.model);
  $contextPressure.set(result.context_pressure ? normalizeContextPressure(result.context_pressure as unknown as Record<string, unknown>) : null);
  $continuedSession.set(result.continued_session);
  $bridge.set(result.bridge ?? {});
  $project.set(result.project);
  $permissions.set(result.permissions);
  $notifications.set(result.notifications || {enabled: true});
  applyNotificationSettings(result.notifications || {enabled: true});
  $slashCommands.set(result.slash_commands);
  ensureStartupLogItem();
  $initialized.set(true);
}

export function applyBridgeEvent(event: BridgeEvent<Record<string, unknown>>) {
  reduceBridgeEvent(event, {
    setForegroundStatus,
    setBusy,
    startTurnTimer,
    finishTurnTimer,
    finishActivity,
    applyUserMessageEvent,
    materializePendingUserMessage,
    removeProvisionalAssistantMessages,
    removeLiveItem,
    coordinatorStatusLabel,
    normalizeContextPressure,
    upsertBackgroundActivity,
    removeBackgroundActivity,
    streamMessageId,
    upsertAssistantDelta,
    upsertAssistantContent,
    commitProvisionalAssistantText,
    upsertTool,
    finishTool,
    appendTranscript,
    failRequest
  });
}

export function appendTranscript(role: TranscriptItem['role'], content: string, id = cryptoRandomId(), options: Partial<TranscriptItem> = {}) {
  commitTranscript(role, content, id, options);
}

function commitTranscript(role: TranscriptItem['role'], content: string, id = cryptoRandomId(), options: Partial<TranscriptItem> = {}) {
  if (!content) {
    return;
  }
  const current = $transcript.get();
  const existingIndex = current.findIndex(item => item.id === id);
  const item: TranscriptItem = {
    id,
    role,
    content,
    state: 'complete',
    ...options,
    sequence: options.sequence ?? (existingIndex >= 0 ? current[existingIndex].sequence : nextLocalSequence())
  };
  if (existingIndex >= 0) {
    setTranscriptItems([...current.slice(0, existingIndex), item, ...current.slice(existingIndex + 1)]);
    return;
  }
  setTranscriptItems([...current, item]);
}

function coordinatorStatusLabel(action: unknown) {
  return action === 'create' ? 'Planning the task...' : 'Updating the task state...';
}

function removeLiveItem(id: string) {
  const current = $transcript.get();
  const next = current.filter(item => item.id !== id);
  if (next.length !== current.length) {
    setTranscriptItems(next);
  }
}

function upsertBackgroundActivity(id: string, kind: BackgroundActivity['kind'], label: string) {
  const current = $backgroundActivities.get();
  const index = current.findIndex(activity => activity.id === id);
  const activity: BackgroundActivity = {
    id,
    kind,
    label,
    startedAt: index >= 0 ? current[index].startedAt : Date.now()
  };
  if (index >= 0) {
    $backgroundActivities.set([...current.slice(0, index), activity, ...current.slice(index + 1)]);
    return;
  }
  $backgroundActivities.set([...current, activity]);
}

function removeBackgroundActivity(id: string) {
  const current = $backgroundActivities.get();
  const next = current.filter(activity => activity.id !== id);
  if (next.length !== current.length) {
    $backgroundActivities.set(next);
  }
}

function normalizeContextPressure(payload: Record<string, unknown>): ContextPressureTelemetry {
  return {
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
  };
}

function optionalNumber(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? number : undefined;
}

function upsertAssistantDelta(messageId: string, delta: string, sequence = 0) {
  if (!delta) {
    return;
  }
  const current = $streamingAssistantBuffers.get();
  const existing = current[messageId] || '';



  const content = `${existing}${delta}`;
  $streamingAssistantBuffers.set({...current, [messageId]: content});
  const transcript = $transcript.get();
  const transcriptIndex = transcript.findIndex(item => item.role === 'assistant' && item.id === messageId && item.state === 'streaming');
  const liveItem: TranscriptItem = {
    id: messageId,
    role: 'assistant',
    content,
    provisional: true,
    markdown: false,
    state: 'streaming',
    sequence: transcriptIndex >= 0 ? transcript[transcriptIndex].sequence : sequence || nextLocalSequence()
  };
  if (transcriptIndex >= 0) {
    setTranscriptItems([...transcript.slice(0, transcriptIndex), liveItem, ...transcript.slice(transcriptIndex + 1)]);
  } else {
    setTranscriptItems([...transcript, liveItem]);
  }
}

function upsertAssistantContent(messageId: string, content: string, fallbackId: string, sequence = 0) {
  if (!content) {
    return;
  }
  $foregroundStatus.set('');
  removeStreamingAssistantBuffer(messageId);
  const current = $transcript.get();
  const index = current.findIndex(item => item.role === 'assistant' && item.id === messageId && item.state === 'streaming');
  if (index >= 0) {
    const existing = current[index];
    const item: TranscriptItem = {
      ...existing,
      content,
      state: 'complete',
      markdown: true,
      provisional: false,
      sequence: existing.sequence ?? sequence
    };
    setTranscriptItems([...current.slice(0, index), item, ...current.slice(index + 1)]);
    return;
  }
  commitTranscript('assistant', content, messageId || fallbackId, {markdown: true, sequence: sequence || undefined});
}

function removeStreamingAssistantBuffer(messageId: string) {
  const current = $streamingAssistantBuffers.get();
  if (!(messageId in current)) {
    return;
  }
  const next = {...current};
  delete next[messageId];
  $streamingAssistantBuffers.set(next);
}

function removeProvisionalAssistantMessages() {
  const current = $transcript.get();
  const next = current.filter(item => !(item.role === 'assistant' && item.provisional && item.stage));
  if (next.length !== current.length) {
    setTranscriptItems(next);
  }
}

function commitProvisionalAssistantText() {
  const buffers = $streamingAssistantBuffers.get();
  const current = $transcript.get();
  const next: TranscriptItem[] = [];
  let changed = false;
  for (const item of current) {
    if (item.role === 'assistant' && item.state === 'streaming' && item.content) {
      next.push({
        ...item,
        id: `${item.id}-pretool-${item.sequence ?? nextLocalSequence()}`,
        state: 'complete',
        provisional: false,
        markdown: false
      });
      changed = true;
    } else {
      next.push(item);
    }
  }
  for (const [messageId, content] of Object.entries(buffers)) {
    if (content && !current.some(item => item.role === 'assistant' && item.state === 'streaming' && item.id === messageId)) {
      const sequence = nextLocalSequence();
      next.push({
        id: `${messageId}-pretool-${sequence}`,
        role: 'assistant',
        content,
        state: 'complete',
        markdown: false,
        sequence
      });
      changed = true;
    }
  }
  if (Object.keys(buffers).length) {
    $streamingAssistantBuffers.set({});
  }
  if (changed) {
    setTranscriptItems(next);
  }
}

function streamMessageId(event: BridgeEvent<Record<string, unknown>>) {
  const messageId = event.payload.message_id;
  return typeof messageId === 'string' && messageId ? messageId : event.event_id;
}

export async function submitPrompt(client: BridgeClient, text: string) {
  const prompt = text.trim();
  if (!prompt) {
    return {shouldExit: false};
  }
  try {
    closeTransientPanelBeforeSubmit();
    if (prompt === '/config') {
      setBusy('loading config', true);
      $config.set((await client.getConfig()) as Record<string, unknown>);
      $activeView.set('config');
      finishActivity('ready');
      return {shouldExit: false};
    }
    if (prompt.startsWith('/')) {
      setBusy('running command', true);
      const result = (await client.executeSlash(prompt)) as Record<string, unknown>;
      applySlashResult(prompt === '/notify' || prompt.startsWith('/notify ')
        ? withLocalNotifyStatus(result)
        : result);
      return {shouldExit: Boolean((result as {should_exit?: boolean}).should_exit)};
    }
    startTurnTimer();
    setBusy('sending', true);
    const visibleOutputStart = $transcript.get().length;
    const pendingId = setPendingUserMessage(prompt);
    await client.sendMessage(prompt);
    materializePendingUserMessage(pendingId);
    appendEmptyTurnNoticeIfNeeded(visibleOutputStart);
    finishTurnTimer();
    return {shouldExit: false};
  } catch (error) {
    materializePendingUserMessage();
    failRequest(error);
    return {shouldExit: false};
  }
}

function setPendingUserMessage(content: string) {
  const id = cryptoRandomId();
  $pendingUserMessage.set({id, content});
  appendTranscript('user', content, id);
  return id;
}

function materializePendingUserMessage(expectedId?: string) {
  const pending = $pendingUserMessage.get();
  if (!pending || (expectedId && pending.id !== expectedId)) {
    return;
  }
  $pendingUserMessage.set(null);
  if (!$transcript.get().some(item => item.id === pending.id && item.role === 'user')) {
    appendTranscript('user', pending.content, pending.id);
  }
}

function appendEmptyTurnNoticeIfNeeded(startIndex: number) {
  const visible = $transcript.get().slice(startIndex);
  if (visible.some(item => item.role === 'assistant' || item.role === 'tool' || item.role === 'system')) {
    return;
  }
  appendTranscript('assistant', 'Sema finished this turn without visible output.');
}

function applyUserMessageEvent(content: string, eventId: string, turnId = '', sequence = 0) {
  const pending = $pendingUserMessage.get();
  if (pending?.content === content) {


    const current = $transcript.get();
    const index = current.findIndex(item => item.id === pending.id && item.role === 'user');
    if (index >= 0 && sequence > 0) {
      const item = current[index];
      if (item.sequence !== sequence || item.turnId !== turnId) {
        setTranscriptItems([
          ...current.slice(0, index),
          {...item, sequence, turnId: turnId || item.turnId},
          ...current.slice(index + 1)
        ]);
      }
    }
    return;
  }
  appendTranscript('user', content, eventId, {turnId, sequence: sequence || undefined});
}

function closeTransientPanelBeforeSubmit() {
  const activeView = $activeView.get();
  if (activeView === 'commandOutput') {
    $commandOutput.set(null);
    $panelScroll.set(0);
    $activeView.set('chat');
  }
}

export async function respondToPendingConfirmation(client: BridgeClient, choice: string, reason = '') {
  const confirmation = $pendingConfirmation.get();
  if (!confirmation) {
    return;
  }
  try {
    await client.respondToConfirmation(confirmation.confirmation_id, choice, reason);
    $pendingConfirmation.set(null);
    $activeView.set('chat');
    setBusy('continuing');
  } catch (error) {
    failRequest(error);
  }
}

export async function cancelTurn(client: BridgeClient) {
  try {
    $cancelRequested.set(true);
    setBusy('cancelling');
    await client.cancelTurn();
  } catch (error) {
    failRequest(error);
  }
}

export async function applyConfigUpdates(client: BridgeClient, updates: Record<string, unknown>) {
  try {
    setBusy('saving config', true);
    $config.set((await client.updateConfig(updates)) as Record<string, unknown>);
    finishActivity('ready');
    return true;
  } catch (error) {
    failRequest(error);
    return false;
  }
}

export function returnToSettings(message: string) {
  notifySettingsResult(message);
  if ($configReturnView.get() === 'setup') {
    $config.set(null);
    $activeView.set('setup');
    return;
  }
  const settings = $settings.get();
  if (!settings) {
    closeActiveView();
    return;
  }
  $config.set(null);
  $activeView.set('settings');
}

export function closeActiveView() {
  if ($pendingConfirmation.get()) {
    return;
  }
  $commandOutput.set(null);
  $config.set(null);
  $settings.set(null);
  $activeView.set('chat');
  $panelScroll.set(0);
}

export function scrollPanel(delta: number) {
  $panelScroll.set(Math.max(0, $panelScroll.get() + delta));
}

export function openLatestToolDetail() {
  const latestTool = [...$tools.get()].reverse().find(tool => tool.status !== 'running') || [...$tools.get()].reverse()[0];
  if (!latestTool) {
    return;
  }
  openCommandOutput(`Tool: ${formatToolName(latestTool.tool)}`, formatToolDetail(latestTool));
}

export async function applyConfigProfile(client: BridgeClient, name: string) {
  try {
    setBusy('saving config', true);
    $config.set((await client.useProfile(name)) as Record<string, unknown>);
    finishActivity('ready');
  } catch (error) {
    failRequest(error);
  }
}

export function failRequest(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  $lastError.set(message);
  finishTurnTimer();
  $status.set('error');
  $busy.set(false);
  $turnStatus.set('idle');
  $foregroundStatus.set('');
  $operationStartedAt.set(null);
}

export function applySlashResult(result: Record<string, unknown>) {
  if (result.view === 'reset') {
    resetLogToStartup();
    const content = String(result.content || '').trim();
    if (content) {
      appendTranscript('system', content);
    }
  } else {
    appendSlashTranscriptItems(result.transcript_items);
    const content = String(result.content || '').trim();
    if (content) {
      openCommandOutput(String(result.title || 'Command output'), content);
    }
  }
  if (result.permissions && typeof result.permissions === 'object') {
    $permissions.set(result.permissions as Record<string, boolean>);
  }
  if (result.notifications && typeof result.notifications === 'object') {
    $notifications.set(result.notifications as Record<string, unknown>);
    applyNotificationSettings(result.notifications);
  }
  if (result.project && typeof result.project === 'object') {
    $project.set(result.project as Record<string, unknown>);
  }
  if (result.session && typeof result.session === 'object') {
    const session = result.session as {continued_session?: boolean};
    if (typeof session.continued_session === 'boolean') {
      $continuedSession.set(session.continued_session);
    }
  }
  if (result.config && typeof result.config === 'object') {
    $config.set(result.config as Record<string, unknown>);
    $activeView.set('config');
  }
  if (result.settings && typeof result.settings === 'object') {
    $settings.set(result.settings as Record<string, unknown>);
    $activeView.set('settings');
  }
  finishActivity('ready');
}

export function applySettingsSnapshot(result: Record<string, unknown>) {
  if (result.settings && typeof result.settings === 'object') {
    $settings.set(result.settings as Record<string, unknown>);
    $activeView.set('settings');
  }
  if (result.permissions && typeof result.permissions === 'object') {
    $permissions.set(result.permissions as Record<string, boolean>);
  }
  if (result.notifications && typeof result.notifications === 'object') {
    $notifications.set(result.notifications as Record<string, unknown>);
    applyNotificationSettings(result.notifications);
  }
  if (Array.isArray(result.slash_commands)) {
    $slashCommands.set(result.slash_commands as SlashCommand[]);
  }
  finishActivity('ready');
}

function appendSlashTranscriptItems(items: unknown) {
  if (!Array.isArray(items)) {
    return;
  }
  for (const item of items) {
    if (!item || typeof item !== 'object') {
      continue;
    }
    const record = item as {role?: string; content?: string; markdown?: boolean};
    const role = record.role === 'assistant' || record.role === 'user' || record.role === 'tool' ? record.role : 'system';
    const content = String(record.content || '').trim();
    if (content) {
      appendTranscript(role, content, undefined, {markdown: Boolean(record.markdown)});
    }
  }
}

export function setBusy(label: string, resetStartedAt = false) {
  $lastError.set('');
  $status.set(label);
  $busy.set(true);
  $operationStartedAt.set(resetStartedAt ? Date.now() : $operationStartedAt.get() ?? Date.now());
}

export function startTurnTimer() {
  if ($turnStartedAt.get() === null) {
    $turnStartedAt.set(Date.now());
  }
}

export function finishTurnTimer() {
  const startedAt = $turnStartedAt.get();
  if (startedAt === null) {
    return;
  }
  $lastTurnDurationMs.set(Math.max(0, Date.now() - startedAt));
  $turnStartedAt.set(null);
}

export function finishActivity(label = 'ready') {
  $lastError.set('');
  $status.set(label);
  $busy.set(false);
  $turnStatus.set('idle');
  $cancelRequested.set(false);
  $foregroundStatus.set('');
  $operationStartedAt.set(null);
}

export function setTranscriptContentHeight(height: number, viewportHeight = 1) {
  const nextHeight = Math.max(0, Math.floor(height));
  $transcriptViewportHeight.set(Math.max(1, Math.floor(viewportHeight)));
  $transcriptContentHeight.set(nextHeight);
  const maxScroll = Math.max(0, nextHeight - viewportHeight);
  if ($transcriptFollow.get()) {
    $transcriptScroll.set(maxScroll);
    return;
  }
  $transcriptScroll.set(Math.min($transcriptScroll.get(), maxScroll));
}

export function registerTranscriptScrollController(controller: TranscriptScrollController | null) {
  transcriptScrollController = controller;
  if (!controller) {
    return () => {};
  }
  const sync = () => {
    $transcriptScroll.set(controller.getScrollTop());
    $transcriptFollow.set(controller.isSticky());
  };
  const unsubscribe = controller.subscribe(sync);
  sync();
  return () => {
    unsubscribe();
    if (transcriptScrollController === controller) {
      transcriptScrollController = null;
    }
  };
}

export function scrollTranscriptBy(delta: number, viewportHeight = 1) {
  if (transcriptScrollController) {
    transcriptScrollController.scrollBy(delta);
    $transcriptFollow.set(false);
    return;
  }
  viewportHeight = viewportHeight > 1 ? viewportHeight : $transcriptViewportHeight.get();
  const maxScroll = Math.max(0, $transcriptContentHeight.get() - viewportHeight);
  const next = Math.max(0, Math.min(maxScroll, $transcriptScroll.get() + Math.trunc(delta)));
  $transcriptScroll.set(next);
  $transcriptFollow.set(next >= maxScroll);
}

export function scrollTranscriptTo(position: 'top' | 'bottom', viewportHeight = 1) {
  if (transcriptScrollController) {
    if (position === 'bottom') {
      transcriptScrollController.scrollToBottom();
      $transcriptFollow.set(true);
    } else {
      transcriptScrollController.scrollTo(0);
      $transcriptFollow.set(false);
    }
    return;
  }
  viewportHeight = viewportHeight > 1 ? viewportHeight : $transcriptViewportHeight.get();
  const maxScroll = Math.max(0, $transcriptContentHeight.get() - viewportHeight);
  $transcriptScroll.set(position === 'top' ? 0 : maxScroll);
  $transcriptFollow.set(position === 'bottom');
}

function finishTool(event: BridgeEvent<Record<string, unknown>>) {
  const tool = String(event.payload.tool ?? 'tool');
  const args = event.payload.args;
  const status = event.payload.status === 'error' ? 'error' : 'ok';
  const itemId = typeof event.payload.item_id === 'string' && event.payload.item_id
    ? event.payload.item_id
    : undefined;
  const current = $tools.get();
  const runningIndex = current.findIndex(item =>
    item.status === 'running' &&
    ((itemId && item.id === itemId) || (!itemId && item.turn_id === event.turn_id && item.tool === tool))
  );
  const record: ToolRecord = {
    id: runningIndex >= 0 ? current[runningIndex].id : itemId || event.event_id,
    turn_id: event.turn_id,
    sequence: runningIndex >= 0 ? current[runningIndex].sequence : eventSequence(event),
    tool,
    args,
    status,
    result: String(event.payload.result ?? ''),
    error: String(event.payload.error ?? '')
  };
  const editStats = normalizeEditStats(event.payload.edit_stats);
  if (editStats) {
    record.edit_stats = editStats;
    addTurnCodeChanges(editStats);
  }
  if (runningIndex >= 0) {
    $tools.set([...current.slice(0, runningIndex), record, ...current.slice(runningIndex + 1)]);
    updateToolTranscript(record);
    return;
  }
  upsertTool(record);
}

function upsertTool(record: ToolRecord) {
  $tools.set([...$tools.get(), record]);
  if (record.status === 'running') {
    upsertLiveTool(record);
    return;
  }
  commitToolTranscript(record);
}

function normalizeEditStats(value: unknown): EditStats | null {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const record = value as Record<string, unknown>;
  const added = Number(record.added ?? 0);
  const removed = Number(record.removed ?? 0);
  if (!Number.isFinite(added) || !Number.isFinite(removed) || (added === 0 && removed === 0)) {
    return null;
  }
  const files = Array.isArray(record.files) ? record.files.map(file => String(file)).filter(Boolean) : [];
  return {
    added,
    removed,
    files,
    tool: String(record.tool ?? '')
  };
}

function addTurnCodeChanges(stats: EditStats) {
  const current = $turnCodeChanges.get();
  const files = Array.from(new Set([...(current.files || []), ...(stats.files || [])]));
  $turnCodeChanges.set({
    added: current.added + stats.added,
    removed: current.removed + stats.removed,
    files
  });
}

function upsertLiveTool(record: ToolRecord) {
  const current = $transcript.get();
  const index = current.findIndex(item => item.role === 'tool' && item.id === record.id);
  const item: TranscriptItem = {
    id: record.id,
    role: 'tool',
    content: formatToolInline(record),
    tool: record,
    state: 'running',
    sequence: index >= 0 ? current[index].sequence : record.sequence || nextLocalSequence()
  };
  if (index >= 0) {
    setTranscriptItems([...current.slice(0, index), item, ...current.slice(index + 1)]);
    return;
  }
  setTranscriptItems([...current, item]);
}

function updateToolTranscript(record: ToolRecord) {
  const current = $transcript.get();
  const index = current.findIndex(item => item.role === 'tool' && item.id === record.id);
  const item: TranscriptItem = {
    id: record.id,
    role: 'tool',
    content: formatToolInline(record),
    tool: record,
    state: 'complete',
    sequence: index >= 0 ? current[index].sequence : nextLocalSequence()
  };
  if (index >= 0) {
    setTranscriptItems([...current.slice(0, index), item, ...current.slice(index + 1)]);
    return;
  }
  setTranscriptItems([...current, item]);
}

function commitToolTranscript(record: ToolRecord, sequence?: number) {
  commitTranscript('tool', formatToolInline(record), record.id, {tool: record, sequence});
}

function openCommandOutput(title: string, content: string) {
  $commandOutput.set({title, content});
  $panelScroll.set(0);
  $activeView.set('commandOutput');
}

function cryptoRandomId() {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
