








import type { Server, Socket } from 'node:net'

import { mkdtempSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { GatewayEvent } from '../gatewayTypes.js'

import { GatewayClientCompat } from '../gatewayClientCompat.js'
import { STUB_SKIN } from '../lib/stubGatewayFixtures.js'
import { RpcClient } from '../rpc/index.js'







type Frame = Record<string, unknown>

interface MockServer {
  socketPath: string
  server: Server
  setHandler: (h: (socket: Socket, frame: Frame) => void) => void
  close: () => Promise<void>
}

function startMock(): Promise<MockServer> {
  return new Promise((resolve, reject) => {
    const dir = mkdtempSync(join(tmpdir(), 'eve-compat-test-'))
    const socketPath = join(dir, 'sock')


    let handler: (socket: Socket, frame: Frame) => void = (socket, frame) => {
      if (frame.method === 'system.hello') {
        const resp = {
          jsonrpc: '2.0',
          id: frame.id,
          result: {
            server_version: '0.0.2',
            server_capabilities: ['cli-dispatch'],
            session: { default_channel: 'tui', default_session_key: 'tui:default' }
          }
        }

        socket.write(JSON.stringify(resp) + '\n')
      }
    }

    const server = createServer(socket => {
      socket.setEncoding('utf-8')
      let readBuf = ''

      socket.on('data', (chunk: string | Buffer) => {
        readBuf += typeof chunk === 'string' ? chunk : chunk.toString('utf-8')
        let nl = readBuf.indexOf('\n')

        while (nl !== -1) {
          const line = readBuf.slice(0, nl).trim()

          readBuf = readBuf.slice(nl + 1)
          if (line.length > 0) {
            try {
              handler(socket, JSON.parse(line) as Frame)
            } catch {
              /* malformed — ignore */
            }
          }

          nl = readBuf.indexOf('\n')
        }
      })
    })

    server.on('error', reject)
    server.listen(socketPath, () => {
      resolve({
        close: () =>
          new Promise<void>(res => {
            server.close(() => {
              rmSync(dir, { force: true, recursive: true })
              res()
            })
          }),
        server,
        setHandler: h => {
          handler = h
        },
        socketPath
      })
    })
  })
}

describe('GatewayClientCompat', () => {
  let mock: MockServer
  let client: GatewayClientCompat | null = null

  beforeEach(async () => {
    mock = await startMock()
  })

  afterEach(async () => {
    if (client) {
      client.kill()
      client = null
    }

    await mock.close()
  })

  it('start() performs system.hello handshake and buffers gateway.ready', async () => {
    client = new GatewayClientCompat({ socketPath: mock.socketPath })

    let helloSeen = false

    mock.setHandler((socket, frame) => {
      if (frame.method === 'system.hello') {
        helloSeen = true
        const params = frame.params as { client_version?: string; client_capabilities?: string[] }

        expect(params.client_version).toBe('0.0.2')
        expect(params.client_capabilities).toContain('cli-dispatch')
        const resp = {
          jsonrpc: '2.0',
          id: frame.id,
          result: {
            server_version: '0.0.2',
            server_capabilities: [],
            session: { default_channel: 'tui', default_session_key: 'tui:default' }
          }
        }

        socket.write(JSON.stringify(resp) + '\n')
      }
    })

    await client.start()
    expect(helloSeen).toBe(true)
  })

  it('drain() after start() replays the synthesized gateway.ready event with STUB_SKIN', async () => {
    client = new GatewayClientCompat({ socketPath: mock.socketPath })
    const events: GatewayEvent[] = []

    client.on('event', (ev: GatewayEvent) => events.push(ev))

    await client.start()

    await new Promise(resolve => setTimeout(resolve, 20))
    client.drain()

    const ready = events.find(e => e.type === 'gateway.ready')

    expect(ready).toBeDefined()
    expect(ready!.payload).toEqual({ skin: STUB_SKIN })
  })

  it('bridges a server-initiated confirm.request notification onto the event bus', async () => {
    client = new GatewayClientCompat({ socketPath: mock.socketPath })
    const events: GatewayEvent[] = []

    client.on('event', (ev: GatewayEvent) => events.push(ev))

    let serverSocket: Socket | null = null
    mock.setHandler((socket, frame) => {
      serverSocket = socket
      if (frame.method === 'system.hello') {
        socket.write(
          JSON.stringify({
            jsonrpc: '2.0',
            id: frame.id,
            result: {
              server_version: '0.0.2',
              server_capabilities: [],
              session: { default_channel: 'tui', default_session_key: 'tui:default' }
            }
          }) + '\n'
        )
      }
    })

    await client.start()
    await new Promise(resolve => setTimeout(resolve, 20))
    client.drain()


    serverSocket!.write(
      JSON.stringify({
        jsonrpc: '2.0',
        method: 'confirm.request',
        params: { default: false, prompt: 'Continue?', request_id: 'r1' }
      }) + '\n'
    )
    await new Promise(resolve => setTimeout(resolve, 20))

    const confirm = events.find(e => e.type === 'confirm.request')

    expect(confirm).toBeDefined()
    expect(confirm!.payload).toEqual({ default: false, prompt: 'Continue?', request_id: 'r1' })
  })

  it('start() is idempotent — second call returns the same promise', async () => {
    client = new GatewayClientCompat({ socketPath: mock.socketPath })

    const p1 = client.start()
    const p2 = client.start()

    expect(p1).toBe(p2)
    await p1
  })

  it('request() delegates to RpcClient.rpc() with method + params', async () => {
    client = new GatewayClientCompat({ socketPath: mock.socketPath })
    await client.start()

    mock.setHandler((socket, frame) => {
      if (frame.method === 'config.get') {
        expect(frame.params).toEqual({ key: 'full' })
        const resp = { jsonrpc: '2.0', id: frame.id, result: { config: { display: {} } } }

        socket.write(JSON.stringify(resp) + '\n')
      }
    })

    const result = await client.request<{ config: { display: Record<string, unknown> } }>('config.get', { key: 'full' })

    expect(result.config.display).toBeDefined()
  })

  it('kill() emits exit event and is safe to call twice', async () => {
    client = new GatewayClientCompat({ socketPath: mock.socketPath })
    await client.start()

    const exits: number[] = []

    client.on('exit', (code: number) => exits.push(code))

    client.kill()
    client.kill()
    expect(exits).toEqual([0])

    const localClient = client

    client = null

    expect(localClient).toBeDefined()
  })

  it('getLogTail() returns a non-empty placeholder string', async () => {
    client = new GatewayClientCompat({ socketPath: mock.socketPath })
    const tail = client.getLogTail()

    expect(typeof tail).toBe('string')
    expect(tail.length).toBeGreaterThan(0)
  })

  it('honors an injected RpcClient (test seam for custom transports)', async () => {
    const rpc = new RpcClient({ socketPath: mock.socketPath })

    client = new GatewayClientCompat({ rpcClient: rpc })

    let helloId: unknown = null

    mock.setHandler((socket, frame) => {
      if (frame.method === 'system.hello') {
        helloId = frame.id
        const resp = {
          jsonrpc: '2.0',
          id: frame.id,
          result: {
            server_version: '0.0.2',
            server_capabilities: [],
            session: { default_channel: 'tui', default_session_key: 'tui:default' }
          }
        }

        socket.write(JSON.stringify(resp) + '\n')
      }
    })

    await client.start()
    expect(helloId).not.toBeNull()
  })
})
