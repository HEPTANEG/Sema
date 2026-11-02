





















import { isXtermJs } from '@hermes/ink'


const WHEEL_ACCEL_WINDOW_MS = 40
const WHEEL_ACCEL_STEP = 0.3
const WHEEL_ACCEL_MAX = 6


const WHEEL_BOUNCE_GAP_MAX_MS = 200
const WHEEL_MODE_STEP = 15
const WHEEL_MODE_CAP = 15
const WHEEL_MODE_RAMP = 3
const WHEEL_MODE_IDLE_DISENGAGE_MS = 1500


const WHEEL_DECAY_HALFLIFE_MS = 150
const WHEEL_DECAY_STEP = 5
const WHEEL_BURST_MS = 5
const WHEEL_DECAY_GAP_MS = 80
const WHEEL_DECAY_CAP_SLOW = 3
const WHEEL_DECAY_CAP_FAST = 6
const WHEEL_DECAY_IDLE_MS = 500

export type WheelAccelState = {
  time: number
  mult: number
  dir: 0 | 1 | -1
  xtermJs: boolean



  frac: number


  base: number


  pendingFlip: boolean


  wheelMode: boolean

  burstCount: number
}

export function initWheelAccel(xtermJs = false, base = 1): WheelAccelState {
  return { burstCount: 0, base, dir: 0, frac: 0, mult: base, pendingFlip: false, time: 0, wheelMode: false, xtermJs }
}



export function readScrollSpeedBase(): number {
  const n = parseFloat(process.env.RAVEN_TUI_SCROLL_SPEED ?? process.env.CLAUDE_CODE_SCROLL_SPEED ?? '')

  return Number.isFinite(n) && n > 0 ? Math.min(n, 20) : 1
}

export function initWheelAccelForHost(): WheelAccelState {
  return initWheelAccel(isXtermJs(), readScrollSpeedBase())
}




export function computeWheelStep(state: WheelAccelState, dir: -1 | 1, now: number): number {
  return state.xtermJs ? xtermJsStep(state, dir, now) : nativeStep(state, dir, now)
}

function nativeStep(state: WheelAccelState, dir: -1 | 1, now: number): number {


  if (state.wheelMode && now - state.time > WHEEL_MODE_IDLE_DISENGAGE_MS) {
    state.wheelMode = false
    state.burstCount = 0
    state.mult = state.base
  }

  if (state.pendingFlip) {
    state.pendingFlip = false

    if (dir !== state.dir || now - state.time > WHEEL_BOUNCE_GAP_MAX_MS) {


      state.dir = dir
      state.time = now
      state.mult = state.base

      return Math.floor(state.mult)
    }

    state.wheelMode = true
  }

  const gap = now - state.time

  if (dir !== state.dir && state.dir !== 0) {
    state.pendingFlip = true
    state.time = now

    return 0
  }

  state.dir = dir
  state.time = now

  if (state.wheelMode) {
    if (gap < WHEEL_BURST_MS) {


      if (++state.burstCount >= 5) {
        state.wheelMode = false
        state.burstCount = 0
        state.mult = state.base
      } else {
        return 1
      }
    } else {
      state.burstCount = 0
    }
  }

  if (state.wheelMode) {
    const m = Math.pow(0.5, gap / WHEEL_DECAY_HALFLIFE_MS)
    const cap = Math.max(WHEEL_MODE_CAP, state.base * 2)
    const next = 1 + (state.mult - 1) * m + WHEEL_MODE_STEP * m

    state.mult = Math.min(cap, next, state.mult + WHEEL_MODE_RAMP)

    return Math.floor(state.mult)
  }



  if (gap > WHEEL_ACCEL_WINDOW_MS) {
    state.mult = state.base
  } else {
    const cap = Math.max(WHEEL_ACCEL_MAX, state.base * 2)

    state.mult = Math.min(cap, state.mult + WHEEL_ACCEL_STEP)
  }

  return Math.floor(state.mult)
}

function xtermJsStep(state: WheelAccelState, dir: -1 | 1, now: number): number {
  const gap = now - state.time
  const sameDir = dir === state.dir

  state.time = now
  state.dir = dir

  if (sameDir && gap < WHEEL_BURST_MS) {
    return 1
  }

  if (!sameDir || gap > WHEEL_DECAY_IDLE_MS) {

    state.mult = 2
    state.frac = 0
  } else {
    const m = Math.pow(0.5, gap / WHEEL_DECAY_HALFLIFE_MS)
    const cap = gap >= WHEEL_DECAY_GAP_MS ? WHEEL_DECAY_CAP_SLOW : WHEEL_DECAY_CAP_FAST

    state.mult = Math.min(cap, 1 + (state.mult - 1) * m + WHEEL_DECAY_STEP * m)
  }

  const total = state.mult + state.frac
  const rows = Math.floor(total)

  state.frac = total - rows

  return rows
}
