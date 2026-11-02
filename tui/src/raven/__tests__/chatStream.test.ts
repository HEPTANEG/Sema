












import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { TurnEvent, TurnSendResult } from '../rpc/index.js'
import type { Msg } from '../types.js'

import { createChatStream, type ChatStreamRpcClient } from '../app/chatStream.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'

interface FakeRpc extends ChatStreamRpcClient {
  __pushEvent: (event: TurnEvent) => void
}

const makeFakeRpc = (sendResult?: TurnSendResult): FakeRpc => {
  let handler: ((event: TurnEvent) => void) | null = null
  const fake: FakeRpc = {
    __pushEvent: (event: TurnEvent) => {
      if (handler) {
        handler(event)
      }
    },
    async rpc<R, P>(method: string, _params: P): Promise<R> {
      if (method === 'turn.send') {
        return (sendResult ?? { turn_id: 'turn-1', accepted: true }) as unknown as R
      }
      if (method === 'turn.cancel') {
        return { cancelled: true } as unknown as R
      }
      return {} as R
    },
    async subscribe<E, P>(_method: string, _params: P, h: (event: E) => void) {
      handler = h as unknown as (event: TurnEvent) => void
      return {
        subscription_id: 'sub-1',
        unsubscribe: async () => {
          handler = null
        }
      }
    }
  }
  return fake
}

describe('createChatStream — wedge defenses', () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
    turnController.fullReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('watchdog clears busy/turnId and surfaces an error when a turn produces no server event', async () => {
    vi.useFakeTimers()
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()



    patchUiState({ busy: true })
    await stream.send('hello')
    expect(stream.isTurnActive()).toBe(true)


    vi.advanceTimersByTime(5000)

    expect(stream.isTurnActive()).toBe(false)
    expect(getUiState().busy).toBe(false)
    expect(sysCalls.some(m => /no response/i.test(m))).toBe(true)
  })

  it('does not fire the watchdog when the turn completes normally', async () => {
    vi.useFakeTimers()
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('hi')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({ type: 'token.delta', payload: { text: 'a' } })
    fake.__pushEvent({
      type: 'message.complete',
      payload: {
        turn_id: 'turn-1',
        usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }
      }
    })


    vi.advanceTimersByTime(60000)
    expect(sysCalls.some(m => /no response/i.test(m))).toBe(false)
    expect(getUiState().busy).toBe(false)
  })

  it('releases busy and surfaces LTM pressure gate RPC errors immediately', async () => {
    vi.useFakeTimers()
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    fake.rpc = vi.fn(async (method: string) => {
      if (method === 'turn.send') {
        throw new Error('LTM maintenance pressure reached 1.0. Run /memory ltm maintain before continuing.')
      }
      return {} as never
    }) as FakeRpc['rpc']
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()
    patchUiState({ busy: true })

    await expect(stream.send('hello')).rejects.toThrow(/LTM maintenance pressure reached 1\.0/)
    vi.advanceTimersByTime(5000)

    expect(stream.isTurnActive()).toBe(false)
    expect(getUiState().busy).toBe(false)
    expect(sysCalls).toEqual([])
  })

  it('forceReset clears turn state locally without any server event', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    await stream.send('long task')
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    patchUiState({ busy: true })
    expect(stream.isTurnActive()).toBe(true)

    stream.forceReset()

    expect(stream.isTurnActive()).toBe(false)
    expect(getUiState().busy).toBe(false)
  })

  it('forceReset clears the armed Ctrl+C escape-hatch flag', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    await stream.send('long task')
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })

    patchUiState({ busy: true, escapeArmed: true })

    stream.forceReset()



    expect(getUiState().escapeArmed).toBe(false)
    expect(getUiState().busy).toBe(false)
  })

  it('does not false-positive when message.start arrives in the same packet as the turn.send accept (arming race)', async () => {
    vi.useFakeTimers()






    let handler: ((event: TurnEvent) => void) | null = null
    const fake: ChatStreamRpcClient = {
      async rpc<R, P>(method: string, _params: P): Promise<R> {
        if (method === 'turn.send') {
          if (handler) {
            handler({ type: 'message.start', payload: { turn_id: 'turn-1' } })
          }
          return { turn_id: 'turn-1', accepted: true } as unknown as R
        }
        return {} as R
      },
      async subscribe<E, P>(_method: string, _params: P, h: (event: E) => void) {
        handler = h as unknown as (event: TurnEvent) => void
        return { subscription_id: 'sub-1', unsubscribe: async () => {} }
      }
    }
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('hello')

    vi.advanceTimersByTime(60000)

    expect(sysCalls.some(m => /no response/i.test(m))).toBe(false)
    expect(stream.isTurnActive()).toBe(true)
  })

  it('recovers the input when turn.send hangs and never returns (hung RPC)', async () => {
    vi.useFakeTimers()


    const fake: ChatStreamRpcClient = {
      async rpc<R, P>(method: string, _params: P): Promise<R> {
        if (method === 'turn.send') {
          return new Promise<R>(() => {})
        }
        return {} as R
      },
      async subscribe<E, P>(_method: string, _params: P, _h: (event: E) => void) {
        return { subscription_id: 'sub-1', unsubscribe: async () => {} }
      }
    }
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()
    patchUiState({ busy: true })
    void stream.send('hello')

    await vi.advanceTimersByTimeAsync(5000)

    expect(getUiState().busy).toBe(false)
    expect(sysCalls.some(m => /no response/i.test(m))).toBe(true)
  })

  it('detach during a hung turn.send clears the in-flight guard so the next send is accepted', async () => {
    let resumeSend: (() => void) | null = null
    const fake: ChatStreamRpcClient = {
      async rpc<R, P>(method: string, _params: P): Promise<R> {
        if (method === 'turn.send') {
          if (resumeSend === null) {

            return new Promise<R>(() => {})
          }
          return { turn_id: 'turn-2', accepted: true } as unknown as R
        }
        return {} as R
      },
      async subscribe<E, P>(_method: string, _params: P, _h: (event: E) => void) {
        return { subscription_id: 'sub-1', unsubscribe: async () => {} }
      }
    }
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    void stream.send('first (hangs)')

    await stream.detach()
    resumeSend = () => {}
    await stream.attach()



    await expect(stream.send('second')).resolves.toMatchObject({ accepted: true })
  })

  it('measures server-ack liveness only: model silence past the window after message.start is not a false positive', async () => {
    vi.useFakeTimers()
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('slow first token')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })


    vi.advanceTimersByTime(60000)

    expect(sysCalls.some(m => /no response/i.test(m))).toBe(false)
    expect(stream.isTurnActive()).toBe(true)
  })
})

describe('createChatStream — cancel preserves streamed content', () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
    turnController.fullReset()
  })

  it('keeps the streamed partial in the transcript on a server cancelled_by_client', async () => {
    const appended: Msg[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: m => appended.push(m),
      rpcClient: fake,
      sessionKey: 'tui:default'
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({ type: 'token.delta', payload: { text: 'Partial answer so far' } })
    fake.__pushEvent({
      type: 'error',
      payload: { code: -32000, message: 'cancelled', reason: 'cancelled_by_client' }
    })

    const assistant = appended.find(m => m.role === 'assistant')
    expect(assistant).toBeDefined()
    expect(assistant!.text).toContain('Partial answer so far')
    expect(assistant!.text).toContain('[interrupted]')
  })

  it('appends the failure detail to the error line when present', async () => {
    const sysCalls: string[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: () => {},
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m)
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({
      type: 'error',
      payload: { code: -32099, message: 'turn_failed', reason: 'internal', detail: "No module named 'orjson'" }
    })

    expect(sysCalls).toContain("error: turn_failed (code=-32099): No module named 'orjson'")
  })

  it('keeps the streamed partial in the transcript on a local forceReset', async () => {
    const appended: Msg[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: m => appended.push(m),
      rpcClient: fake,
      sessionKey: 'tui:default'
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({ type: 'token.delta', payload: { text: 'Half a reply' } })

    stream.forceReset()

    const assistant = appended.find(m => m.role === 'assistant')
    expect(assistant).toBeDefined()
    expect(assistant!.text).toContain('Half a reply')
    expect(assistant!.text).toContain('[interrupted]')
  })

  it('does not emit a redundant interrupted note when a second cancel re-enters after content was already preserved', async () => {
    const appended: Msg[] = []
    const sysCalls: string[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: m => appended.push(m),
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m)
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({ type: 'token.delta', payload: { text: 'A streamed reply' } })


    stream.forceReset()


    fake.__pushEvent({
      type: 'error',
      payload: { code: -32000, message: 'cancelled', reason: 'cancelled_by_client' }
    })


    expect(appended.filter(m => m.role === 'assistant')).toHaveLength(1)
    expect(sysCalls.filter(m => m === 'interrupted')).toHaveLength(0)
  })

  it('does not append an empty assistant message when nothing was streamed', async () => {
    const appended: Msg[] = []
    const sysCalls: string[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: m => appended.push(m),
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m)
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })

    stream.forceReset()

    expect(appended.some(m => m.role === 'assistant')).toBe(false)
    expect(sysCalls).toContain('interrupted')
  })
})

describe('createChatStream — cron.missed startup notice', () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
    turnController.fullReset()
  })

  it('renders one compact summary block via sys with per-item name and scheduled HH:MM', async () => {
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m)
    })
    await stream.attach()

    fake.__pushEvent({
      type: 'cron.missed',
      payload: {
        count: 2,
        items: [
          { message: '记得喝水', name: 'hydrate', scheduled_at: '2025-06-04T10:03:00+00:00' },
          { message: '起来活动一下', name: 'stretch', scheduled_at: '2025-06-04T10:40:00+00:00' }
        ]
      }
    })

    expect(sysCalls).toHaveLength(1)
    const block = sysCalls[0]
    expect(block).toContain('missed 2 reminders')



    expect(block).toMatch(/hydrate — scheduled \d{2}-\d{2} \d{2}:\d{2}: 记得喝水/)
    expect(block).toMatch(/stretch — scheduled \d{2}-\d{2} \d{2}:\d{2}: 起来活动一下/)


    expect(block).toMatch(/scheduled \d{2}-\d{2} \d{2}:\d{2}/)

    expect(stream.isTurnActive()).toBe(false)
  })

  it('renders a bare HH:MM when the missed reminder was scheduled today', async () => {
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m)
    })
    await stream.attach()

    const today = new Date()
    today.setHours(9, 30, 0, 0)
    fake.__pushEvent({
      type: 'cron.missed',
      payload: {
        count: 1,
        items: [{ message: 'drink water', name: 'hydrate', scheduled_at: today.toISOString() }]
      }
    })

    expect(sysCalls).toHaveLength(1)
    expect(sysCalls[0]).toContain('hydrate — scheduled 09:30: drink water')
    expect(sysCalls[0]).not.toMatch(/scheduled \d{2}-\d{2} /)
  })
})
