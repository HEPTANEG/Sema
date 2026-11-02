import notifier from 'node-notifier';
import type {BridgeEvent} from './protocol/types.js';

type Notifier = {
  notify: (options: {title: string; message: string; wait?: boolean}) => void;
};

export type FocusTrackingStatus = 'unknown' | 'enabled' | 'unavailable';

const SUMMARY_LIMIT = 120;
const FOCUS_IN = '\u001b[I';
const FOCUS_OUT = '\u001b[O';

let notificationsEnabled = true;
let terminalFocused = true;
let focusTrackingStatus: FocusTrackingStatus = 'unknown';
let notifierImpl: Notifier = notifier;
const notifiedMessageIds = new Set<string>();

export function applyNotificationSettings(settings: unknown) {
  if (!settings || typeof settings !== 'object') {
    return;
  }
  const record = settings as {enabled?: unknown};
  notificationsEnabled = record.enabled !== false;
}

export function notificationStatus() {
  return {
    enabled: notificationsEnabled,
    terminalFocused,
    focusTrackingStatus
  };
}

export function setNotifierForTests(next: Notifier) {
  notifierImpl = next;
}

export function setTerminalFocusedForTests(next: boolean) {
  terminalFocused = next;
}

export function resetNotificationsForTests() {
  notificationsEnabled = true;
  terminalFocused = true;
  focusTrackingStatus = 'unknown';
  notifiedMessageIds.clear();
  notifierImpl = notifier;
}

export function enableTerminalFocusTracking(input = process.stdin, output = process.stdout) {
  if (!input.isTTY || !output.isTTY) {
    focusTrackingStatus = 'unavailable';
    return () => {};
  }


  output.write('\u001b[?1004h');
  focusTrackingStatus = 'enabled';

  const onData = (chunk: Buffer | string) => {
    const text = String(chunk);
    if (text.includes(FOCUS_IN)) {
      terminalFocused = true;
    }
    if (text.includes(FOCUS_OUT)) {
      terminalFocused = false;
    }
  };
  input.on('data', onData);

  return () => {
    input.off('data', onData);
    output.write('\u001b[?1004l');
    focusTrackingStatus = 'unknown';
  };
}

export function enableBracketedPaste(output = process.stdout) {
  if (!output.isTTY) {
    return () => {};
  }
  output.write('\u001b[?2004h');
  return () => {
    output.write('\u001b[?2004l');
  };
}

export function maybeNotifyBridgeEvent(event: BridgeEvent<Record<string, unknown>>) {
  if (!notificationsEnabled || terminalFocused) {
    return;
  }
  const notification = notificationForBridgeEvent(event);
  if (!notification) {
    return;
  }
  if (notification.key) {
    if (notifiedMessageIds.has(notification.key)) {
      return;
    }
    notifiedMessageIds.add(notification.key);
  }
  notifierImpl.notify({
    title: notification.title,
    message: summarize(notification.message),
    wait: false
  });
}

export function notifySettingsResult(message: string) {
  if (!notificationsEnabled) return;
  notifierImpl.notify({title: 'Sema settings', message: summarize(message), wait: false});
}

export function notificationForBridgeEvent(event: BridgeEvent<Record<string, unknown>>) {
  const payload = event.payload || {};
  if (event.type === 'final_answer' || event.type === 'assistant_content') {
    return {
      title: 'Sema replied',
      message: String(payload.content || 'Sema has a new response.'),
      key: messageNotificationKey(event)
    };
  }
  if (event.type === 'confirmation_requested') {
    return {
      title: 'Sema needs confirmation',
      message: [payload.title, payload.details].map(value => String(value || '').trim()).filter(Boolean).join(' - ') || 'Sema is waiting for your confirmation.',
      key: event.event_id
    };
  }
  if (event.type === 'agent_error' || event.type === 'error') {
    return {
      title: 'Sema error',
      message: String(payload.error || payload.message || 'Sema hit an error.'),
      key: event.event_id
    };
  }
  if (event.type === 'agent_stopped') {
    return {
      title: 'Sema stopped',
      message: String(payload.message || 'Sema stopped this turn.'),
      key: event.event_id
    };
  }
  if (event.type === 'turn_cancelled') {
    return {
      title: 'Sema cancelled',
      message: String(payload.message || 'Sema cancelled this turn.'),
      key: event.event_id
    };
  }
  return null;
}

function messageNotificationKey(event: BridgeEvent<Record<string, unknown>>) {
  const messageId = event.payload?.message_id;
  return typeof messageId === 'string' && messageId ? `message:${messageId}` : event.event_id;
}

function summarize(value: string) {
  const normalized = String(value || '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!normalized) {
    return 'Sema has a new update.';
  }
  return normalized.length > SUMMARY_LIMIT ? `${normalized.slice(0, SUMMARY_LIMIT - 1)}…` : normalized;
}
