

































import { EventEmitter } from 'node:events'

import type { GatewayEvent } from './gatewayTypes.js'

import { STUB_SKIN } from './lib/stubGatewayFixtures.js'
import { RpcClient } from './rpc/index.js'

const CLIENT_VERSION = '0.0.2'



const CLIENT_CAPABILITIES: string[] = ['cli-dispatch', 'cli', 'config', 'stubs']

export interface GatewayClientCompatOptions {

  socketPath?: string

  rpcClient?: RpcClient
}

export class GatewayClientCompat extends EventEmitter {







  public readonly rpcClient: RpcClient
  private bufferedEvents: GatewayEvent[] = []
  private subscribed = false
  private startPromise: Promise<void> | null = null
  private killed = false

  constructor(opts: GatewayClientCompatOptions = {}) {
    super()
    this.setMaxListeners(0)
    this.rpcClient =
      opts.rpcClient ??
      new RpcClient({




        onNotification: (method, params) => this.publishServerNotification(method, params),
        socketPath: opts.socketPath
      })
  }






  private publishServerNotification(method: string, params: unknown): void {
    this.publish({ payload: params, type: method } as GatewayEvent)
  }











  start(): Promise<void> {
    if (this.startPromise) {
      return this.startPromise
    }

    this.startPromise = (async () => {
      await this.rpcClient.rpc('system.hello', {
        client_version: CLIENT_VERSION,
        client_capabilities: CLIENT_CAPABILITIES
      })




      setTimeout(() => {
        if (this.killed) {
          return
        }

        const ready: GatewayEvent = { payload: { skin: STUB_SKIN }, type: 'gateway.ready' }
        this.publish(ready)
      }, 0)
    })()

    return this.startPromise
  }







  drain(): void {
    this.subscribed = true
    const queued = this.bufferedEvents
    this.bufferedEvents = []

    for (const ev of queued) {
      this.emit('event', ev)
    }
  }


  async request<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    return this.rpcClient.rpc<T>(method, params)
  }





  kill(): void {
    if (this.killed) {
      return
    }

    this.killed = true
    this.bufferedEvents = []
    this.subscribed = false
    try {
      this.rpcClient.close()
    } finally {
      this.emit('exit', 0)
    }
  }






  getLogTail(_limit = 20): string {
    return '(real gateway via RPC — see Python loguru sink for server logs)'
  }

  private publish(ev: GatewayEvent): void {
    if (this.subscribed) {
      this.emit('event', ev)
    } else {
      this.bufferedEvents.push(ev)
    }
  }
}







export type GatewayClient = GatewayClientCompat
