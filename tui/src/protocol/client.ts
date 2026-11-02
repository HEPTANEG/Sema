import {createHash} from 'node:crypto';
import {spawn, type ChildProcess} from 'node:child_process';
import {EventEmitter} from 'node:events';
import {resolve} from 'node:path';
import {createConnection, type Socket} from 'node:net';
import type {BridgeEvent, InitializeResult, JsonRpcId, JsonRpcResponse, LastRunResult, SessionState, TrajectoryListResult} from './types.js';

type PendingRequest = {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
};

export class BridgeClient extends EventEmitter {
  private child?: ChildProcess;
  private socket?: Socket;
  private nextId = 1;
  private pending = new Map<JsonRpcId, PendingRequest>();
  private lineBuffer = '';
  private ready: Promise<void>;
  private reconnecting?: Promise<void>;
  private disposed = false;
  private exitEmitted = false;
  private restarting = false;
  private readonly command: string;
  private readonly args: string[];
  private readonly workspace: string;
  private readonly endpoint: string;

  constructor(command = 'python', args = ['my_agent/main.py', 'backend']) {
    super();
    this.command = command;
    this.args = args;
    this.workspace = resolve(process.env.SEMA_TUI_WORKSPACE_CWD || process.cwd());
    this.endpoint = namedPipeForWorkspace(this.workspace);
    this.ready = this.connectOrStart(command, args);
  }

  async request<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    await this.ready;
    const socket = await this.ensureConnected();
    {
      const id = this.nextId++;
      const payload = {jsonrpc: '2.0', id, method, params};
      const promise = new Promise<T>((resolve, reject) => {
        this.pending.set(id, {
          resolve: value => resolve(value as T),
          reject
        });
      });
      try {
        socket.write(`${JSON.stringify(payload)}\n`, error => {
          if (!error) {
            return;
          }
          const pending = this.pending.get(id);
          if (pending) {
            this.pending.delete(id);
            pending.reject(error);
          }
        });
      } catch (error) {
        this.pending.delete(id);
        throw error;
      }
      return promise;
    }
  }

  initialize(): Promise<InitializeResult> {
    return this.request<InitializeResult>('initialize');
  }

  sendMessage(prompt: string) {
    return this.request('message.send', {prompt, source: 'tui'});
  }

  cancelTurn() {
    return this.request('turn.cancel');
  }

  getSessionState() {
    return this.request<SessionState>('session.state');
  }

  getLastRun() {
    return this.request<LastRunResult>('lastRun.get');
  }

  listTrajectory(mode = '', limit = 20) {
    return this.request<TrajectoryListResult>('trajectory.list', {mode, limit});
  }

  updatePermissions(action: 'reset') {
    return this.request('permissions.update', {action});
  }

  getPermissions() {
    return this.request('permissions.get');
  }

  executeSlash(command: string) {
    return this.request('slash.execute', {command});
  }

  respondToConfirmation(confirmation_id: string, choice: string, reason = '') {
    return this.request('confirmation.respond', {confirmation_id, choice, reason});
  }

  getConfig() {
    return this.request('config.get');
  }

  async restartBackend(): Promise<InitializeResult> {
    this.restarting = true;
    try {
      await this.request('backend.restart');
      for (let attempt = 0; attempt < 100 && (this.isSocketWritable() || this.isChildAlive()); attempt++) {
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      if (this.isSocketWritable() || this.isChildAlive()) throw new Error('Backend has not stopped yet.');
      this.lineBuffer = '';
      await this.ensureConnected();
      const result = await this.initialize();
      this.emit('reinitialized', result);
      return result;
    } finally {this.restarting = false;}
  }

  updateConfig(updates: Record<string, unknown>) {
    return this.request('config.update', {updates});
  }

  updateSettings(action: string) {
    return this.request('settings.update', {action});
  }

  useProfile(name: string) {
    return this.request('config.useProfile', {name});
  }

  dispose() {
    this.disposed = true;
    this.socket?.destroy();
  }

  private async connectOrStart(command: string, args: string[]) {
    try {
      await this.connect(this.endpoint);
      return;
    } catch {
      this.startBackend(command, args);
    }

    let lastError: unknown;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      try {
        await this.connect(this.endpoint);
        return;
      } catch (error) {
        lastError = error;
        await new Promise(resolveDelay => setTimeout(resolveDelay, 100));
      }
    }
    throw new Error(`Sema Backend did not start: ${String(lastError)}`);
  }

  private async ensureConnected() {
    if (!this.endpoint) {
      throw new Error('Sema Backend connection is unavailable. Restart sema and try again.')
    }

    if (this.isSocketWritable()) {
      return this.socket as Socket;
    }

    if (!this.reconnecting) {
      this.reconnecting = this.reconnect().finally(() => {
        this.reconnecting = undefined;
      });
    }

    await this.reconnecting;
    const socket = this.socket;
    if (!socket || socket.destroyed || !socket.writable) {
      throw new Error('Sema Backend connection is unavailable. Restart sema and try again.');
    }
    return socket;
  }

  private async reconnect() {
    let lastError: unknown;
    for (let attempt = 0; attempt < 10; attempt += 1) {
      try {
        await this.connect(this.endpoint);
        return;
      } catch (error) {
        lastError = error;
        await new Promise(resolveDelay => setTimeout(resolveDelay, 100));
      }
    }

    if (!this.isChildAlive()) {
      this.startBackend(this.command, this.args);
      for (let attempt = 0; attempt < 40; attempt += 1) {
        try {
          await this.connect(this.endpoint);
          return;
        } catch (error) {
          lastError = error;
          await new Promise(resolveDelay => setTimeout(resolveDelay, 100));
        }
      }
    }

    throw new Error(`Sema Backend connection is unavailable. Restart sema and try again. (${String(lastError)})`);
  }

  private startBackend(command: string, args: string[]) {
    if (this.isChildAlive()) {
      return;
    }

    this.child = spawn(command, args, {
      cwd: process.cwd(),
      env: {...process.env, SEMA_TUI_WORKSPACE_CWD: this.workspace},
      stdio: ['ignore', 'ignore', 'pipe'],
      windowsHide: true
    });
    this.child.stderr?.on('data', chunk => this.emit('stderr', String(chunk)));
    this.child.on('exit', (code, signal) => {
      if (this.disposed || this.restarting || this.isSocketWritable()) {
        return;
      }
      const error = new Error(`Sema backend exited: code=${code ?? 'null'} signal=${signal ?? 'null'}`);
      this.rejectPending(error);
      this.emitExitOnce({code, signal});
    });
  }

  private isChildAlive() {
    return Boolean(this.child && this.child.exitCode === null && !this.child.killed);
  }

  private connect(endpoint: string) {
    return new Promise<void>((resolveConnection, reject) => {
      const socket = createConnection(endpoint);
      const fail = (error: Error) => {
        socket.destroy();
        reject(error);
      };
      socket.once('connect', () => {
        socket.removeListener('error', fail);
        this.lineBuffer = '';
        this.exitEmitted = false;
        this.socket = socket;
        let connectionClosed = false;
        const connectionLost = (error: Error) => {
          if (connectionClosed) {
            return;
          }
          connectionClosed = true;
          if (this.socket === socket) {
            this.socket = undefined;
          }
          if (!this.disposed) {
            this.rejectPending(error);
            if (!this.restarting) {
              this.emitExitOnce({code: null, signal: error.message});
              void this.ensureConnected().then(() => this.initialize()).then(result => this.emit('reinitialized', result)).catch(() => {});
            }
          }
        };
        socket.on('data', chunk => this.handleData(String(chunk)));
        socket.on('error', connectionLost);
        socket.on('close', () => connectionLost(new Error('Sema Backend connection closed.')));
        resolveConnection();
      });
      socket.once('error', fail);
    });
  }

  private handleData(data: string) {
    this.lineBuffer += data;
    while (this.lineBuffer.includes('\n')) {
      const newline = this.lineBuffer.indexOf('\n');
      const line = this.lineBuffer.slice(0, newline);
      this.lineBuffer = this.lineBuffer.slice(newline + 1);
      if (line.trim()) {
        this.handleLine(line);
      }
    }
  }

  private rejectPending(error: Error) {
    for (const pending of this.pending.values()) {
      pending.reject(error);
    }
    this.pending.clear();
  }

  private isSocketWritable() {
    return Boolean(this.socket && !this.socket.destroyed && this.socket.writable);
  }

  private emitExitOnce(payload: {code: number | null; signal: NodeJS.Signals | string | null}) {
    if (this.exitEmitted) {
      return;
    }
    this.exitEmitted = true;
    this.emit('exit', payload);
  }

  private handleLine(line: string) {
    const parsed = parseBridgeLine(line);
    if (!parsed.ok) {
      this.emit('malformed', {line, error: parsed.error});
      return;
    }
    const payload = parsed.payload;

    if ('method' in payload && payload.method?.startsWith('event.')) {
      this.emit('event', payload.params);
      this.emit(payload.method, payload.params);
      return;
    }

    if ('id' in payload) {
      const pending = this.pending.get(payload.id);
      if (!pending) {
        return;
      }
      this.pending.delete(payload.id);
      if (payload.error) {
        const error = new Error(payload.error.message);
        this.emit('request_error', error);
        pending.reject(error);
      } else {
        pending.resolve(payload.result);
      }
    }
  }
}

function namedPipeForWorkspace(workspace: string) {
  const digest = createHash('sha256').update(workspace.toLowerCase()).digest('hex').slice(0, 24);
  return `\\\\.\\pipe\\sema-backend-${digest}`;
}

export function parseBridgeLine(line: string):
  | {ok: true; payload: JsonRpcResponse | {method?: string; params?: BridgeEvent}}
  | {ok: false; error: unknown} {
  try {
    return {ok: true, payload: JSON.parse(line)};
  } catch (error) {
    return {ok: false, error};
  }
}
