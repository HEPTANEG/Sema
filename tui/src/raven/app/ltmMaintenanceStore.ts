



import { atom } from 'nanostores'

import type { Msg } from '../types.js'

export type LtmMaintenanceStatus = 'completed' | 'failed' | 'idle' | 'running'

export interface LtmMaintenanceState {
  active: boolean
  historyItems: Msg[]
  lastResult: Record<string, unknown> | null
  runId: null | string
  startedAt: null | number
  stage: null | string
  stageIndex: null | number
  status: LtmMaintenanceStatus
  summary: string
  terminal: boolean
}

const initialState = (): LtmMaintenanceState => ({
  active: false,
  historyItems: [],
  lastResult: null,
  runId: null,
  startedAt: null,
  stage: null,
  stageIndex: null,
  status: 'idle',
  summary: '',
  terminal: false
})

export const $ltmMaintenanceState = atom<LtmMaintenanceState>(initialState())

export const getLtmMaintenanceState = () => $ltmMaintenanceState.get()

export const patchLtmMaintenanceState = (
  next: Partial<LtmMaintenanceState> | ((state: LtmMaintenanceState) => LtmMaintenanceState)
) => $ltmMaintenanceState.set(typeof next === 'function' ? next($ltmMaintenanceState.get()) : { ...$ltmMaintenanceState.get(), ...next })

export const resetLtmMaintenanceState = () => $ltmMaintenanceState.set(initialState())

export const openLtmMaintenanceView = (command: string) => {
  $ltmMaintenanceState.set({
    active: true,
    historyItems: [
      { kind: 'slash', role: 'system', text: command },
      { role: 'system', text: 'LTM Maintainer started. Waiting for runtime events…' }
    ],
    lastResult: null,
    runId: null,
    startedAt: Date.now(),
    stage: null,
    stageIndex: null,
    status: 'running',
    summary: '',
    terminal: false
  })
}

export const appendLtmMaintenanceMessage = (msg: Msg) =>
  patchLtmMaintenanceState(state => ({ ...state, historyItems: [...state.historyItems, msg] }))

export const closeLtmMaintenanceView = () =>
  patchLtmMaintenanceState(state => ({
    ...state,
    active: false
  }))
