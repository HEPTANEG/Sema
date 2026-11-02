import {afterEach, expect, it, vi} from 'vitest';
import {applyNotificationSettings, maybeNotifyBridgeEvent, notifySettingsResult, resetNotificationsForTests, setNotifierForTests, setTerminalFocusedForTests} from './notifications.js';

afterEach(resetNotificationsForTests);

it('notifies once for a reply when unfocused', () => {
  const notify = vi.fn();
  setNotifierForTests({notify});
  const event = {event_id: 'reply', turn_id: 'turn', type: 'final_answer', payload: {content: 'Hello'}};
  maybeNotifyBridgeEvent(event);
  expect(notify).not.toHaveBeenCalled();
  setTerminalFocusedForTests(false);
  maybeNotifyBridgeEvent(event);
  maybeNotifyBridgeEvent(event);
  expect(notify).toHaveBeenCalledTimes(1);
  expect(notify).toHaveBeenCalledWith({title: 'Sema replied', message: 'Hello', wait: false});
});

it('allows foreground settings feedback but respects the notification switch', () => {
  const notify = vi.fn();
  setNotifierForTests({notify});
  notifySettingsResult('New settings are now active.');
  expect(notify).toHaveBeenCalledTimes(1);
  applyNotificationSettings({enabled: false});
  notifySettingsResult('Changes were not saved.');
  expect(notify).toHaveBeenCalledTimes(1);
});
