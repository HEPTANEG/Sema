import React from 'react';
import {PassThrough} from 'node:stream';
import {afterEach, expect, it, vi} from 'vitest';
import {renderSync} from '@hermes/ink';
import {SetupPanel} from './SetupPanel.js';
import {$activeView, $setup, $setupPage, $terminalRows} from '../stores/runtime.js';
import {resetNotificationsForTests, setNotifierForTests} from '../notifications.js';
import type {BridgeClient} from '../protocol/client.js';

afterEach(() => {$setup.set(null); $activeView.set('chat'); resetNotificationsForTests();});

it('renders setup choices without invoking expensive tests', async () => {
  $setupPage.set('overview'); $terminalRows.set(40);
  const request = vi.fn().mockResolvedValue({components: {embedding: {status: 'Missing'}, reranker: {status: 'Missing'}, llm: {status: 'Not tested'}}});
  const mounted = renderPanel({request} as unknown as BridgeClient);
  try {
    await tick();
    expect(mounted.output()).toContain('Embedding');
    expect(mounted.output()).toContain('Reranker');
    expect(mounted.output()).toContain('Continue without LTM');
    expect(request.mock.calls.every(call => call[0] === 'setup.status')).toBe(true);
  } finally {mounted.cleanup();}
});

it('Cancel returns to Settings without saving or leaving persistent feedback', async () => {
  $setupPage.set('search'); $terminalRows.set(40); $activeView.set('setup');
  const snapshot = {components: {search: {status: 'Ready', values: {}, secrets: {api_key: true}, fields: [{key: 'api_key', label: 'API key', secret: true}]}}};
  $setup.set(snapshot);
  const notify = vi.fn(); setNotifierForTests({notify});
  const request = vi.fn().mockResolvedValue(snapshot);
  const mounted = renderPanel({request} as unknown as BridgeClient);
  try {
    await tick();
    for (let i = 0; i < 3; i++) {mounted.input('\u001b[B'); await tick();}
    mounted.input('\r'); await tick();
    expect($activeView.get()).toBe('settings');
    expect(request.mock.calls.some(call => call[0] === 'settings.update')).toBe(false);
    expect(notify).toHaveBeenCalled();
    expect(mounted.output()).not.toContain('Changes were not saved.');
  } finally {mounted.cleanup();}
});

const tick = () => new Promise(resolve => setTimeout(resolve, 25));

it('masks secret entry and only sends it on Confirm', async () => {
  $setupPage.set('search'); $terminalRows.set(40); $activeView.set('setup');
  const snapshot = {components: {search: {status: 'Missing', values: {}, fields: [{key: 'api_key', label: 'API key', secret: true}]}}};
  $setup.set(snapshot);
  setNotifierForTests({notify: vi.fn()});
  const request = vi.fn().mockResolvedValue(snapshot);
  const mounted = renderPanel({request} as unknown as BridgeClient);
  try {
    await tick(); mounted.input('\r'); await tick();
    mounted.input('private-test-secret'); await tick();
    expect(mounted.output()).not.toContain('private-test-secret');
    mounted.input('\r'); await tick();
    expect(request.mock.calls.some(call => call[0] === 'settings.update')).toBe(false);
    for (let i = 0; i < 4; i++) {mounted.input('\u001b[B'); await tick();}
    mounted.input('\r'); await tick();
    expect(request).toHaveBeenCalledWith('settings.update', {component: 'search', updates: {api_key: 'private-test-secret', clear_api_key: false}});
    expect($activeView.get()).toBe('settings');
  } finally {mounted.cleanup();}
});

function renderPanel(client: BridgeClient) {
  const stdout = Object.assign(new PassThrough(), {columns: 110, rows: 40, isTTY: true});
  const stdin = Object.assign(new PassThrough(), {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stderr = Object.assign(new PassThrough(), {isTTY: true});
  let text = '';
  stdout.on('data', chunk => {text += String(chunk);});
  const instance = renderSync(<SetupPanel client={client} />, {stdout: stdout as unknown as NodeJS.WriteStream, stdin: stdin as unknown as NodeJS.ReadStream, stderr: stderr as unknown as NodeJS.WriteStream, patchConsole: false});
  return {input: (value: string) => stdin.write(value), output: () => text.replace(/\u001b\[1C/g, ' '), cleanup: () => {instance.unmount(); instance.cleanup();}};
}
