




















import type {
  ErrorEvent,
  MessageCompleteEvent,
  MessageStartEvent,
  TokenDeltaEvent,
  ToolCompleteEvent,
  ToolStartEvent,
  TurnEvent,
  TurnSendParams,
  TurnSendResult,
  TurnSubscribeParams
} from '../rpc/index.js'
import type { Msg } from '../types.js'

import { argPreview } from '../lib/toolArgs.js'
import { turnController } from './turnController.js'
import { patchTurnState } from './turnStore.js'
import { patchUiState } from './uiStore.js'







export interface ChatStreamRpcClient {
  rpc<R = unknown, P = unknown>(method: string, params: P): Promise<R>
  subscribe<E = unknown, P = unknown>(
    method: string,
    params: P,
    handler: (event: E) => void,
    opts?: { unsubscribeMethod?: string }
  ): Promise<{ subscription_id: string; unsubscribe: () => Promise<void> }>
}

export interface ChatStreamOptions {
  rpcClient: ChatStreamRpcClient
  sessionKey: string

  sys?: (msg: string) => void






  appendMessage?: (msg: Msg) => void








  watchdogMs?: number
}


export const DEFAULT_WATCHDOG_MS = 10_000

export interface ChatStreamHandle {
  attach: () => Promise<void>
  detach: () => Promise<void>
  send: (content: string) => Promise<TurnSendResult>
  cancel: () => Promise<void>
  isTurnActive: () => boolean





  forceReset: () => void
}

interface InternalState {
  attached: boolean
  unsubscribe: (() => Promise<void>) | null
  turnId: string | null
}






const formatScheduledAt = (iso: string): string => {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) {
    return iso
  }
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
  if (sameDay) {
    return hhmm
  }
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hhmm}`
}

const dispatch = (
  state: InternalState,
  event: TurnEvent,
  sys?: (msg: string) => void,
  appendMessage?: (msg: Msg) => void
): void => {
  switch (event.type) {
    case 'message.start':
      onMessageStart(state, event)
      return
    case 'episode.start':
      turnController.recordEpisodeStart(event.payload.index)
      return
    case 'token.delta':
      onTokenDelta(event)
      return
    case 'thinking.delta':



      turnController.recordReasoningDelta(event.payload.text)
      return
    case 'tool.start':
      onToolStart(event)
      return
    case 'tool.progress':


      return
    case 'tool.complete':
      onToolComplete(event)
      return
    case 'message.complete':
      onMessageComplete(state, event, appendMessage)
      return
    case 'error':
      onError(state, event, sys, appendMessage)
      return
    case 'cron.delivered': {
      if (sys) {
        const { name, text, fired_at } = event.payload
        const tag = fired_at ? `${name} @ ${fired_at}` : name
        sys(`─── ⏰ ${tag} ───\n${text}\n${'─'.repeat(40)}`)
      }
      return
    }
    case 'cron.missed': {
      if (sys) {
        const { count, items } = event.payload
        const lines = items.map(item => `${item.name} — scheduled ${formatScheduledAt(item.scheduled_at)}: ${item.message}`)
        const noun = count === 1 ? 'reminder' : 'reminders'
        sys(`─── ⏰ missed ${count} ${noun} ───\n${lines.join('\n')}\n${'─'.repeat(40)}`)
      }
      return
    }
    default: {


      const exhaustive: never = event
      void exhaustive
    }
  }
}

const onMessageStart = (state: InternalState, ev: MessageStartEvent): void => {
  state.turnId = ev.payload.turn_id
  turnController.startMessage()
  patchUiState({ status: 'running…' })
}

const onTokenDelta = (ev: TokenDeltaEvent): void => {
  turnController.recordMessageDelta({ text: ev.payload.text })
}

const onToolStart = (ev: ToolStartEvent): void => {
  const { tool_call_id, name, arguments: args, display } = ev.payload


  turnController.recordToolStart(tool_call_id, name, display ?? argPreview(args))
}

const onToolComplete = (ev: ToolCompleteEvent): void => {
  const { tool_call_id, result_preview, truncated } = ev.payload
  const summary = truncated ? `${result_preview} (truncated)` : result_preview
  turnController.recordToolComplete(tool_call_id, undefined, undefined, summary)
}

const onMessageComplete = (
  state: InternalState,
  ev: MessageCompleteEvent,
  appendMessage?: (msg: Msg) => void
): void => {
  state.turnId = null






  if (ev.payload.usage) {
    patchUiState(s => ({ ...s, usage: { ...s.usage, ...ev.payload.usage } }))
  }
  const { finalMessages, finalText, wasInterrupted } = turnController.recordMessageComplete({})
  if (!wasInterrupted && appendMessage) {
    const msgs: Msg[] = finalMessages.length > 0 ? finalMessages : [{ role: 'assistant', text: finalText }]
    msgs.forEach(appendMessage)
  }
  patchUiState({ status: 'ready' })
}

const onError = (
  state: InternalState,
  ev: ErrorEvent,
  sys?: (msg: string) => void,
  appendMessage?: (msg: Msg) => void
): void => {
  const { reason, message, code, detail } = ev.payload
  state.turnId = null
  if (reason === 'cancelled_by_client') {
    restoreInputPrompt(appendMessage, sys)
    return
  }




  if (sys) {
    const extra = detail ? `: ${detail.split('\n')[0].slice(0, 200)}` : ''
    sys(`error: ${message} (code=${code})${extra}`)
  }
  turnController.recordError()
  patchUiState({ busy: false, status: `error: ${message.slice(0, 80)}` })
  patchTurnState({ activity: [], outcome: '' })
}

const restoreInputPrompt = (appendMessage?: (msg: Msg) => void, sys?: (msg: string) => void): void => {






  turnController.finalizeInterruptedTurn({ appendMessage, sys })
  turnController.clearStatusTimer()
  patchUiState({ busy: false, status: 'interrupted' })


  setTimeout(() => {
    patchUiState({ status: 'ready' })
  }, 800)
}

export const createChatStream = (opts: ChatStreamOptions): ChatStreamHandle => {
  const state: InternalState = {
    attached: false,
    unsubscribe: null,
    turnId: null
  }

  const watchdogMs = opts.watchdogMs ?? DEFAULT_WATCHDOG_MS
  let watchdog: ReturnType<typeof setTimeout> | null = null




  let sendInFlight = false

  const clearWatchdog = (): void => {
    if (watchdog !== null) {
      clearTimeout(watchdog)
      watchdog = null
    }
  }

  const forceReset = (): void => {



    clearWatchdog()
    sendInFlight = false
    state.turnId = null
    restoreInputPrompt(opts.appendMessage, opts.sys)
  }

  const armAckWatchdog = (): void => {
    clearWatchdog()









    watchdog = setTimeout(() => {
      if (!sendInFlight && state.turnId === null) {
        return
      }
      if (opts.sys) {
        opts.sys('turn produced no response — input restored (press Enter to retry)')
      }
      forceReset()
    }, watchdogMs)
  }

  const attach = async (): Promise<void> => {
    if (state.attached) {
      return
    }
    const params: TurnSubscribeParams = { session_key: opts.sessionKey }



    const result = await opts.rpcClient.subscribe<TurnEvent, TurnSubscribeParams>(
      'turn.subscribe',
      params,
      event => {



        clearWatchdog()
        dispatch(state, event, opts.sys, opts.appendMessage)
      },
      { unsubscribeMethod: 'turn.unsubscribe' }
    )
    state.unsubscribe = result.unsubscribe
    state.attached = true
  }

  const detach = async (): Promise<void> => {
    if (!state.attached) {
      return
    }
    clearWatchdog()
    sendInFlight = false
    const u = state.unsubscribe
    state.unsubscribe = null
    state.attached = false
    state.turnId = null
    if (u) {
      await u()
    }
  }

  const send = async (content: string): Promise<TurnSendResult> => {
    if (state.turnId || sendInFlight) {
      throw new Error('turn already in progress — wait for message.complete or cancel first')
    }
    const params: TurnSendParams = {
      session_key: opts.sessionKey,
      content
    }



    sendInFlight = true
    armAckWatchdog()
    let result: TurnSendResult
    try {
      result = await opts.rpcClient.rpc<TurnSendResult, TurnSendParams>('turn.send', params)
    } catch (err) {
      sendInFlight = false
      clearWatchdog()
      patchUiState({ busy: false, status: 'ready' })
      throw err
    }
    sendInFlight = false





    if (result.accepted) {
      state.turnId = result.turn_id
    } else {
      clearWatchdog()
    }
    return result
  }

  const cancel = async (): Promise<void> => {
    if (!state.turnId) {
      return
    }
    await opts.rpcClient.rpc<{ cancelled: boolean }, { session_key: string }>('turn.cancel', {
      session_key: opts.sessionKey
    })
    // We do NOT clear state.turnId here — the server is expected to emit an
    // `error(reason=cancelled_by_client)` event that drives the actual
    // UI-state reset via dispatch(). Clearing locally would race with the
    // event delivery and leave the turn-active guard inconsistent.
  }

  const isTurnActive = (): boolean => state.turnId !== null

  return { attach, detach, send, cancel, isTurnActive, forceReset }
}
