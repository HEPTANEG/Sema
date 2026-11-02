import { atom, computed } from 'nanostores'

import type { GatewaySkin } from '../gatewayTypes.js'

import { MOUSE_TRACKING } from '../config/env.js'
import { ZERO } from '../domain/usage.js'
import { applyDetectedBackground, DEFAULT_THEME, fromSkin, resolveCurrentDefaultTheme } from '../theme.js'
import { DEFAULT_INDICATOR_STYLE, type UiState } from './interfaces.js'

const buildUiState = (): UiState => ({
  bgTasks: new Set(),
  busy: false,
  busyInputMode: 'queue',
  compact: false,
  escapeArmed: false,
  detailsMode: 'collapsed',
  detailsModeCommandOverride: false,
  activeFocusTitle: null,
  focusShiftNotice: null,
  indicatorStyle: DEFAULT_INDICATOR_STYLE,
  info: null,
  inlineDiffs: true,
  mouseTracking: MOUSE_TRACKING,
  sections: {},
  showCost: false,



  showReasoning: true,
  sid: null,
  status: 'summoning raven…',
  statusBar: 'bottom',
  streaming: true,
  transcript: 'episodes',
  theme: DEFAULT_THEME,
  usage: ZERO
})

export const $uiState = atom<UiState>(buildUiState())

export const $uiTheme = computed($uiState, state => state.theme)
export const $uiSessionId = computed($uiState, state => state.sid)

export const getUiState = () => $uiState.get()

export const patchUiState = (next: Partial<UiState> | ((state: UiState) => UiState)) =>
  $uiState.set(typeof next === 'function' ? next($uiState.get()) : { ...$uiState.get(), ...next })

export const resetUiState = () => $uiState.set(buildUiState())



let lastSkin: GatewaySkin | null = null

const buildSkinTheme = (s: GatewaySkin) =>
  fromSkin(
    s.colors ?? {},
    s.branding ?? {},
    s.banner_logo ?? '',
    s.banner_hero ?? '',
    s.tool_prefix ?? '',
    s.help_header ?? ''
  )

export const applySkinTheme = (s: GatewaySkin) => {
  lastSkin = s
  patchUiState({ theme: buildSkinTheme(s) })
}







export const applyTerminalBackground = (oscData: string) => {
  const res = applyDetectedBackground(oscData)

  if (res?.changed) {
    patchUiState({ theme: lastSkin ? buildSkinTheme(lastSkin) : resolveCurrentDefaultTheme() })
  }

  return res
}
