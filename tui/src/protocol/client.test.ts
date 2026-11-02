import {describe, expect, it} from 'vitest';
import {BridgeClient, parseBridgeLine} from './client.js';

describe('parseBridgeLine', () => {
  it('parses valid json-rpc lines', () => {
    expect(parseBridgeLine('{"jsonrpc":"2.0","id":1,"result":{"ok":true}}')).toMatchObject({
      ok: true
    });
  });

  it('marks malformed json', () => {
    expect(parseBridgeLine('{nope')).toMatchObject({ok: false});
  });
});

describe('BridgeClient helpers', () => {
  it('reinitializes after restart without replaying previous requests', async () => {
    const client = Object.create(BridgeClient.prototype) as any;
    const calls: string[] = [];
    const result = {setup: {ltm_enabled: true}};
    client.request = async (method: string) => {calls.push(method); return method === 'initialize' ? result : {restarting: true};};
    client.isSocketWritable = () => false;
    client.isChildAlive = () => false;
    client.ensureConnected = async () => {calls.push('connect');};
    client.emit = (event: string, value: unknown) => {calls.push(event); expect(value).toBe(result);};
    expect(await client.restartBackend()).toBe(result);
    expect(calls).toEqual(['backend.restart', 'connect', 'initialize', 'reinitialized']);
    expect(client.restarting).toBe(false);
  });

  it('maps structured helper methods to json-rpc requests', async () => {
    const calls: Array<[string, Record<string, unknown>?]> = [];
    const client = Object.create(BridgeClient.prototype) as BridgeClient & {
      request: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;
    };
    client.request = async <T = unknown>(method: string, params?: Record<string, unknown>) => {
      calls.push([method, params]);
      return {} as T;
    };

    await client.getLastRun();
    await client.listTrajectory('current', 5);
    await client.getPermissions();
    await client.updatePermissions('reset');

    expect(calls).toEqual([
      ['lastRun.get', undefined],
      ['trajectory.list', {mode: 'current', limit: 5}],
      ['permissions.get', undefined],
      ['permissions.update', {action: 'reset'}]
    ]);
  });

  it('rejects immediately when the backend socket is unavailable', async () => {
    const client = Object.create(BridgeClient.prototype) as any;
    client.ready = Promise.resolve();
    client.nextId = 1;
    client.pending = new Map();

    await expect(client.request('session.state')).rejects.toThrow('connection is unavailable');
    expect(client.pending.size).toBe(0);
  });
});
