

















import type { Socket } from 'node:net'

import { createConnection } from 'node:net'

import type { EventNotificationParams, JsonRpcErrorResponse, JsonRpcRequest, JsonRpcResponse } from './generated.js'

import { rpcErrorFromFrame } from './errors.js'
import { isJsonRpcError } from './generated.js'
import { SubscriptionRegistry } from './subscriptions.js'

const MAX_FRAME_BYTES = 1024 * 1024

type Pending = {
  resolve: (value: unknown) => void
  reject: (err: Error) => void
}

export interface RpcClientOptions {

  socketPath?: string

  warn?: (msg: string) => void







  onNotification?: (method: string, params: unknown) => void
}

export class RpcClient {
  private readonly socket: Socket
  private readonly registry = new SubscriptionRegistry()
  private readonly pending = new Map<number, Pending>()
  private readonly warn: (msg: string) => void
  private readonly onNotification?: (method: string, params: unknown) => void

  private nextId = 1
  private readBuffer = ''
  private writeQueue: Promise<void> = Promise.resolve()
  private closed = false
  private connected = false
  private readonly connectPromise: Promise<void>

  constructor(opts: RpcClientOptions = {}) {
    const target = opts.socketPath ?? process.env.RAVEN_RPC_SOCKET
    if (!target) {
      throw new Error('RpcClient: no RPC target supplied; pass `socketPath` or set ' + 'RAVEN_RPC_SOCKET env var.')
    }
    this.warn = opts.warn ?? (m => process.stderr.write(`[rpc-client] ${m}\n`))
    this.onNotification = opts.onNotification




    const tcp = /^(.+):(\d+)$/.exec(target)
    if (tcp) {
      this.socket = createConnection({ host: tcp[1], port: Number(tcp[2]) })
    } else {
      this.socket = createConnection(target)
    }
    this.socket.setEncoding('utf-8')



    const authToken = process.env.RAVEN_RPC_TOKEN

    this.connectPromise = new Promise<void>((resolve, reject) => {
      const onConnect = () => {
        this.connected = true
        this.socket.off('error', onError)

        if (authToken) {
          this.socket.write(authToken + '\n')
        }
        resolve()
      }
      const onError = (err: Error) => {
        this.socket.off('connect', onConnect)
        reject(err)
      }
      this.socket.once('connect', onConnect)
      this.socket.once('error', onError)
    })

    this.socket.on('data', (chunk: string | Buffer) => {
      const text = typeof chunk === 'string' ? chunk : chunk.toString('utf-8')
      this.readBuffer += text
      if (this.readBuffer.length > MAX_FRAME_BYTES * 2) {

        this.warn(
          `incoming read buffer exceeded ${MAX_FRAME_BYTES * 2} bytes without ` + 'newline — closing connection'
        )
        this.failAll(new Error('rpc-client: frame size limit exceeded'))
        this.socket.destroy()
        return
      }
      this.drainBuffer()
    })
    this.socket.on('end', () => this.failAll(new Error('socket closed by peer')))
    this.socket.on('error', err => this.failAll(err))
  }


  ready(): Promise<void> {
    return this.connectPromise
  }

  private drainBuffer(): void {
    let nl = this.readBuffer.indexOf('\n')
    while (nl !== -1) {
      const line = this.readBuffer.slice(0, nl).trim()
      this.readBuffer = this.readBuffer.slice(nl + 1)
      if (line.length > 0) {
        this.handleFrame(line)
      }
      nl = this.readBuffer.indexOf('\n')
    }
  }

  private handleFrame(line: string): void {
    if (line.length > MAX_FRAME_BYTES) {
      this.warn(`oversized frame (${line.length} bytes) dropped`)
      return
    }
    let frame: unknown
    try {
      frame = JSON.parse(line)
    } catch {
      this.warn(`malformed frame ignored: ${line.slice(0, 120)}`)
      return
    }
    if (!frame || typeof frame !== 'object') {
      this.warn('non-object frame ignored')
      return
    }
    const obj = frame as Record<string, unknown>


    if (obj.id === undefined && typeof obj.method === 'string') {
      if (obj.method === 'event') {
        const params = obj.params as EventNotificationParams<unknown> | undefined
        if (params && typeof params.subscription_id === 'string') {
          this.registry.dispatch(params)
        } else {
          this.warn('event notification missing subscription_id/event')
        }
      } else if (this.onNotification) {


        this.onNotification(obj.method, obj.params)
      } else {
        this.warn(`unknown notification method: ${obj.method}`)
      }
      return
    }


    const resp = frame as JsonRpcResponse<unknown>
    const id = resp.id
    if (typeof id !== 'number' && typeof id !== 'string') {
      this.warn('response frame has no valid id')
      return
    }
    const idKey = typeof id === 'number' ? id : Number(id)
    const pending = this.pending.get(idKey)
    if (!pending) {
      this.warn(`response for unknown id ${String(id)}`)
      return
    }
    this.pending.delete(idKey)
    if (isJsonRpcError(resp)) {
      pending.reject(rpcErrorFromFrame((resp as JsonRpcErrorResponse).error))
    } else {
      pending.resolve(resp.result)
    }
  }

  private failAll(err: Error): void {
    if (this.closed) {
      return
    }
    this.closed = true
    for (const [, p] of this.pending) {
      p.reject(err)
    }
    this.pending.clear()
    this.registry.clear()
  }

  private async writeFrame(frame: string): Promise<void> {
    if (frame.length > MAX_FRAME_BYTES) {
      throw new Error(`rpc-client: outgoing frame ${frame.length} bytes exceeds ${MAX_FRAME_BYTES} limit`)
    }


    const prev = this.writeQueue
    this.writeQueue = (async () => {
      await prev
      if (!this.connected) {
        await this.connectPromise
      }
      await new Promise<void>((resolve, reject) => {
        this.socket.write(frame, err => (err ? reject(err) : resolve()))
      })
    })()
    return this.writeQueue
  }


  async rpc<R = unknown, P = unknown>(method: string, params: P): Promise<R> {
    if (this.closed) {
      throw new Error('rpc-client: closed')
    }
    const id = this.nextId++
    const req: JsonRpcRequest<P> = { jsonrpc: '2.0', id, method, params }
    const frame = JSON.stringify(req) + '\n'
    const result = new Promise<R>((resolve, reject) => {
      this.pending.set(id, {
        resolve: v => resolve(v as R),
        reject
      })
    })
    try {
      await this.writeFrame(frame)
    } catch (err) {
      this.pending.delete(id)
      throw err
    }
    return result
  }









  async subscribe<E = unknown, P = unknown, R extends { subscription_id: string } = { subscription_id: string }>(
    method: string,
    params: P,
    handler: (event: E) => void,
    opts: { unsubscribeMethod?: string } = {}
  ): Promise<{ subscription_id: string; unsubscribe: () => Promise<void> }> {
    const result = await this.rpc<R, P>(method, params)
    const subscriptionId = result.subscription_id
    this.registry.register<E>(subscriptionId, handler)
    const unsubscribeMethod = opts.unsubscribeMethod
    const unsubscribe = async (): Promise<void> => {
      this.registry.unregister(subscriptionId)
      if (unsubscribeMethod && !this.closed) {
        await this.rpc<unknown, { subscription_id: string }>(unsubscribeMethod, {
          subscription_id: subscriptionId
        })
      }
    }
    return { subscription_id: subscriptionId, unsubscribe }
  }


  pendingCount(): number {
    return this.pending.size
  }


  subscriptionCount(): number {
    return this.registry.size()
  }


  close(): void {
    this.failAll(new Error('rpc-client: closed by caller'))
    try {
      this.socket.end()
    } catch {
      /* noop */
    }
    try {
      this.socket.destroy()
    } catch {
      /* noop */
    }
  }
}
