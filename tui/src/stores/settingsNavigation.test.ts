import {afterEach, expect, it, vi} from 'vitest';
import {$activeView, $config, $model, $settings, applyConfigUpdates, closeActiveView, returnToSettings} from './runtime.js';
import type {BridgeClient} from '../protocol/client.js';
import {resetNotificationsForTests, setNotifierForTests} from '../notifications.js';

afterEach(() => {closeActiveView(); resetNotificationsForTests();});

it('returns from the model editor to settings with cancellation feedback', () => {
  const notify = vi.fn();
  setNotifierForTests({notify});
  $settings.set({categories: [{id: 'model'}]});
  $config.set({config: {model: 'saved'}});
  $activeView.set('config');
  returnToSettings('Changes were not saved.');
  expect($activeView.get()).toBe('settings');
  expect($config.get()).toBeNull();
  expect($settings.get()).toEqual({categories: [{id: 'model'}]});
  expect(notify).toHaveBeenCalledWith({title: 'Sema settings', message: 'Changes were not saved.', wait: false});
});

it('reports a failed config save without closing the editor', async () => {
  $settings.set({categories: []});
  $config.set({config: {model: 'saved'}});
  $activeView.set('config');
  const client = {updateConfig: vi.fn().mockRejectedValue(new Error('Save failed'))} as unknown as BridgeClient;
  expect(await applyConfigUpdates(client, {model: 'new'})).toBe(false);
  expect($activeView.get()).toBe('config');
  expect($config.get()).toEqual({config: {model: 'saved'}});
});

it('updates the chat model label after a successful config save', async () => {
  const client = {updateConfig: vi.fn().mockResolvedValue({config: {provider: 'deepseek', model: 'deepseek-flash'}})} as unknown as BridgeClient;
  expect(await applyConfigUpdates(client, {model: 'deepseek-flash'})).toBe(true);
  expect($model.get()).toBe('deepseek:deepseek-flash');
});
