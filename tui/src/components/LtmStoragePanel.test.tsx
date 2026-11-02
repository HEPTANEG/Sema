import React from 'react';
import {PassThrough} from 'node:stream';
import {expect, it, vi} from 'vitest';
import {renderSync} from '@hermes/ink';
import {LtmStoragePanel} from './LtmStoragePanel.js';
import type {BridgeClient} from '../protocol/client.js';

const status = {db_path: 'test/ltm.db', live_object_count: 3, deleted_object_count: 2,
  database_bytes: 1048576, vector_bytes: 0, warning_mib: null, over_warning: false};
const tick = () => new Promise(resolve => setTimeout(resolve, 30));

it('requires a preview and explicit second confirmation before purge', async () => {
  const request = vi.fn(async (method: string) => method === 'ltm.storage.preview'
    ? {preview_token: 'fixed-batch', object_count: 2, db_path: 'test/ltm.db', warning: 'Irreversible'}
    : method === 'ltm.storage.purge' ? {status: 'purged', removed_count: 2} : status);
  const mounted = renderPanel({request} as unknown as BridgeClient);
  try {
    await tick();
    expect(mounted.output()).toContain('Logically deleted: 2');
    mounted.input('\u001b[B'); await tick(); mounted.input('\r'); await tick();
    expect(request).toHaveBeenCalledWith('ltm.storage.preview', {});
    expect(request.mock.calls.some(call => call[0] === 'ltm.storage.purge')).toBe(false);
    mounted.input('\u001b[C'); await tick(); mounted.input('\r'); await tick();
    expect(request).toHaveBeenCalledWith('ltm.storage.purge', {preview_token: 'fixed-batch', confirmed: true});
  } finally {mounted.cleanup();}
});

it('saves MiB warning without any cleanup request', async () => {
  const request = vi.fn().mockResolvedValue(status);
  const mounted = renderPanel({request} as unknown as BridgeClient);
  try {
    await tick(); mounted.input('\r'); await tick(); mounted.input('256'); await tick(); mounted.input('\r'); await tick();
    expect(request).toHaveBeenCalledWith('ltm.storage.warning', {warning_mib: 256});
    expect(request.mock.calls.some(call => call[0] === 'ltm.storage.purge')).toBe(false);
  } finally {mounted.cleanup();}
});

function renderPanel(client: BridgeClient) {
  const stdout = Object.assign(new PassThrough(), {columns: 110, rows: 40, isTTY: true});
  const stdin = Object.assign(new PassThrough(), {isTTY: true, setRawMode() {}, ref() {}, unref() {}});
  const stderr = Object.assign(new PassThrough(), {isTTY: true});
  let text = '';
  stdout.on('data', chunk => {text += String(chunk);});
  const instance = renderSync(<LtmStoragePanel client={client} onBack={() => {}} />, {stdout: stdout as unknown as NodeJS.WriteStream, stdin: stdin as unknown as NodeJS.ReadStream, stderr: stderr as unknown as NodeJS.WriteStream, patchConsole: false});
  return {input: (value: string) => stdin.write(value), output: () => text.replace(/\u001b\[1C/g, ' '), cleanup: () => {instance.unmount(); instance.cleanup();}};
}
