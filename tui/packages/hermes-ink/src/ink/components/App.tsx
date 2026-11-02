





import { appendFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { PureComponent, type ReactNode } from 'react'

import { updateLastInteractionTime } from '../../bootstrap/state.js'
import { logForDebugging } from '../../utils/debug.js'
import { env } from '../../utils/env.js'
import { stopCapturingEarlyInput } from '../../utils/earlyInput.js'
import { isMouseClicksDisabled } from '../../utils/fullscreen.js'
import { logError } from '../../utils/log.js'
import type { DOMElement } from '../dom.js'
import { EventEmitter } from '../events/emitter.js'
import { InputEvent } from '../events/input-event.js'
import { TerminalFocusEvent } from '../events/terminal-focus-event.js'
import {
  INITIAL_STATE,
  type ParsedInput,
  type ParsedKey,
  type ParsedMouse,
  parseMultipleKeypresses
} from '../parse-keypress.js'
import reconciler from '../reconciler.js'
import { finishSelection, hasSelection, type SelectionState, startSelection } from '../selection.js'
import { getTerminalFocused, setTerminalFocused } from '../terminal-focus-state.js'
import { TerminalQuerier, xtversion } from '../terminal-querier.js'
import {
  isExtendedKeysCapableByXtversion,
  isXtermJs,
  setXtversionName,
  supportsExtendedKeys
} from '../terminal.js'
import {
  DISABLE_KITTY_KEYBOARD,
  DISABLE_MODIFY_OTHER_KEYS,
  ENABLE_KITTY_KEYBOARD,
  ENABLE_MODIFY_OTHER_KEYS,
  FOCUS_IN,
  FOCUS_OUT
} from '../termio/csi.js'
import { DBP, DFE, DISABLE_MOUSE_TRACKING, EBP, EFE, SHOW_CURSOR } from '../termio/dec.js'

import AppContext from './AppContext.js'
import { ClockProvider } from './ClockContext.js'
import CursorDeclarationContext, { type CursorDeclarationSetter } from './CursorDeclarationContext.js'
import ErrorOverview from './ErrorOverview.js'
import StdinContext from './StdinContext.js'
import { TerminalFocusProvider } from './TerminalFocusContext.js'
import { TerminalSizeContext } from './TerminalSizeContext.js'


const SUPPORTS_SUSPEND = false






const STDIN_RESUME_GAP_MS = 5000
type Props = {
  readonly children: ReactNode
  readonly stdin: NodeJS.ReadStream
  readonly stdout: NodeJS.WriteStream
  readonly stderr: NodeJS.WriteStream
  readonly exitOnCtrlC: boolean
  readonly onExit: (error?: Error) => void
  readonly terminalColumns: number
  readonly terminalRows: number




  readonly selection: SelectionState
  readonly onSelectionChange: () => void




  readonly onClickAt: (col: number, row: number) => boolean
  readonly onMouseDownAt: (col: number, row: number, button: number) => DOMElement | undefined
  readonly onMouseUpAt: (target: DOMElement, col: number, row: number, button: number) => void
  readonly onMouseDragAt: (target: DOMElement, col: number, row: number, button: number) => void



  readonly onHoverAt: (col: number, row: number) => void



  readonly getHyperlinkAt: (col: number, row: number) => string | undefined

  readonly onOpenHyperlink: (url: string) => void




  readonly onMultiClick: (col: number, row: number, count: 2 | 3) => void



  readonly onSelectionDrag: (col: number, row: number) => void




  readonly onStdinResume?: () => void




  readonly onCursorDeclaration?: CursorDeclarationSetter


  readonly dispatchKeyboardEvent: (parsedKey: ParsedKey) => void
}



const MULTI_CLICK_TIMEOUT_MS = 500
const MULTI_CLICK_DISTANCE = 1
type State = {
  readonly error?: Error
}




export default class App extends PureComponent<Props, State> {
  static displayName = 'InternalApp'
  static getDerivedStateFromError(error: Error) {
    return {
      error
    }
  }
  override state = {
    error: undefined
  }



  rawModeEnabledCount = 0




  extendedKeysPushed = false
  inputEmitter = new EventEmitter()
  keyParseState = INITIAL_STATE

  incompleteEscapeTimer: NodeJS.Timeout | null = null

  readonly NORMAL_TIMEOUT = 50
  readonly PASTE_TIMEOUT = 500



  querier = new TerminalQuerier(this.props.stdout)




  lastClickTime = 0
  lastClickCol = -1
  lastClickRow = -1
  clickCount = 0




  pendingHyperlinkTimer: ReturnType<typeof setTimeout> | null = null



  lastHoverCol = -1
  lastHoverRow = -1
  mouseCaptureTarget: DOMElement | undefined




  lastStdinTime = Date.now()


  isRawModeSupported(): boolean {
    return this.props.stdin.isTTY
  }
  override render() {
    return (
      <TerminalSizeContext.Provider
        value={{
          columns: this.props.terminalColumns,
          rows: this.props.terminalRows
        }}
      >
        <AppContext.Provider
          value={{
            exit: this.handleExit
          }}
        >
          <StdinContext.Provider
            value={{
              stdin: this.props.stdin,
              setRawMode: this.handleSetRawMode,
              isRawModeSupported: this.isRawModeSupported(),
              exitOnCtrlC: this.props.exitOnCtrlC,
              inputEmitter: this.inputEmitter,
              querier: this.querier
            }}
          >
            <TerminalFocusProvider>
              <ClockProvider>
                <CursorDeclarationContext.Provider value={this.props.onCursorDeclaration ?? (() => {})}>
                  {this.state.error ? <ErrorOverview error={this.state.error as Error} /> : this.props.children}
                </CursorDeclarationContext.Provider>
              </ClockProvider>
            </TerminalFocusProvider>
          </StdinContext.Provider>
        </AppContext.Provider>
      </TerminalSizeContext.Provider>
    )
  }
  override componentWillUnmount() {
    if (this.props.stdout.isTTY) {
      this.props.stdout.write(SHOW_CURSOR)
    }


    if (this.incompleteEscapeTimer) {
      clearTimeout(this.incompleteEscapeTimer)
      this.incompleteEscapeTimer = null
    }

    if (this.pendingHyperlinkTimer) {
      clearTimeout(this.pendingHyperlinkTimer)
      this.pendingHyperlinkTimer = null
    }


    if (this.isRawModeSupported()) {
      this.handleSetRawMode(false)
    }
  }
  override componentDidCatch(error: Error) {
    this.handleExit(error)
  }
  handleSetRawMode = (isEnabled: boolean): void => {
    const { stdin } = this.props

    if (!this.isRawModeSupported()) {
      if (stdin === process.stdin) {
        throw new Error(
          'Raw mode is not supported on the current process.stdin, which Ink uses as input stream by default.\nRead about how to prevent this error on https://github.com/vadimdemedes/ink/#israwmodesupported'
        )
      } else {
        throw new Error(
          'Raw mode is not supported on the stdin provided to Ink.\nRead about how to prevent this error on https://github.com/vadimdemedes/ink/#israwmodesupported'
        )
      }
    }

    stdin.setEncoding('utf8')

    if (isEnabled) {

      if (this.rawModeEnabledCount === 0) {




        stopCapturingEarlyInput()
        stdin.ref()
        stdin.setRawMode(true)
        stdin.addListener('readable', this.handleReadable)

        this.props.stdout.write(EBP)

        this.props.stdout.write(EFE)






        const _supportsExtKeys = supportsExtendedKeys()
        if (_supportsExtKeys) {
          this.props.stdout.write(ENABLE_KITTY_KEYBOARD)
          this.props.stdout.write(ENABLE_MODIFY_OTHER_KEYS)
          this.extendedKeysPushed = true
        }





        if (process.env.RAVEN_TUI_DEBUG_TERMINFO === '1') {
          try {
            const diagPath = join(homedir(), '.raven', 'logs', 'tui-node-diag.log')
            const line = JSON.stringify({
              ts: new Date().toISOString(),
              phase: 'raw-mode-enable',
              TERM_PROGRAM: process.env.TERM_PROGRAM ?? null,
              TERM: process.env.TERM ?? null,
              ZELLIJ: process.env.ZELLIJ ? 'set' : 'unset',
              WT_SESSION: process.env.WT_SESSION ? 'set' : 'unset',
              KITTY_WINDOW_ID: process.env.KITTY_WINDOW_ID ? 'set' : 'unset',
              TMUX: process.env.TMUX ? 'set' : 'unset',
              STY: process.env.STY ? 'set' : 'unset',
              CURSOR_TRACE_ID: process.env.CURSOR_TRACE_ID ? 'set' : 'unset',
              'env.terminal': env.terminal,
              'supportsExtendedKeys()': _supportsExtKeys,
              kittyPushedAtEnable: _supportsExtKeys
            })
            appendFileSync(diagPath, line + '\n', { mode: 0o600 })
          } catch {
            // diag write failure must not break TUI startup
          }
        }









        setImmediate(() => {
          void Promise.all([this.querier.send(xtversion()), this.querier.flush()]).then(([r]) => {
            let rePushed = false

            if (r) {
              setXtversionName(r.name)
              logForDebugging(`XTVERSION: terminal identified as "${r.name}"`)









              if (!this.extendedKeysPushed && isExtendedKeysCapableByXtversion(r.name)) {
                this.props.stdout.write(ENABLE_KITTY_KEYBOARD)
                this.props.stdout.write(ENABLE_MODIFY_OTHER_KEYS)
                this.extendedKeysPushed = true
                rePushed = true
              }
            } else {
              logForDebugging('XTVERSION: no reply (terminal ignored query)')
            }

            if (process.env.RAVEN_TUI_DEBUG_TERMINFO === '1') {
              try {
                const diagPath = join(homedir(), '.raven', 'logs', 'tui-node-diag.log')
                const line = JSON.stringify({
                  ts: new Date().toISOString(),
                  phase: 'xtversion-reply',
                  xtversionName: r?.name ?? null,
                  replyReceived: Boolean(r),
                  rePushed,
                  extendedKeysPushedAfter: this.extendedKeysPushed
                })
                appendFileSync(diagPath, line + '\n', { mode: 0o600 })
              } catch {
                // diag write failure must not break TUI startup
              }
            }
          })
        })
      }

      this.rawModeEnabledCount++

      return
    }


    if (--this.rawModeEnabledCount === 0) {
      this.props.stdout.write(DISABLE_MODIFY_OTHER_KEYS)
      this.props.stdout.write(DISABLE_KITTY_KEYBOARD)

      this.props.stdout.write(DFE)

      this.props.stdout.write(DBP)
      stdin.setRawMode(false)
      stdin.removeListener('readable', this.handleReadable)
      stdin.unref()
    }
  }


  flushIncomplete = (): void => {

    this.incompleteEscapeTimer = null




    if (!this.keyParseState.incomplete && this.keyParseState.mode !== 'IN_PASTE') {
      return
    }









    if (this.props.stdin.readableLength > 0) {
      this.incompleteEscapeTimer = setTimeout(
        this.flushIncomplete,
        this.keyParseState.mode === 'IN_PASTE' ? this.PASTE_TIMEOUT : this.NORMAL_TIMEOUT
      )

      return
    }



    this.processInput(null)
  }


  processInput = (input: string | Buffer | null): void => {

    const [keys, newState] = parseMultipleKeypresses(this.keyParseState, input)
    this.keyParseState = newState






    if (keys.length > 0) {
      reconciler.discreteUpdates(processKeysInBatch, this, keys, undefined, undefined)
    }




    if (this.keyParseState.incomplete || this.keyParseState.mode === 'IN_PASTE') {

      if (this.incompleteEscapeTimer) {
        clearTimeout(this.incompleteEscapeTimer)
      }

      this.incompleteEscapeTimer = setTimeout(
        this.flushIncomplete,
        this.keyParseState.mode === 'IN_PASTE' ? this.PASTE_TIMEOUT : this.NORMAL_TIMEOUT
      )
    }
  }
  handleReadable = (): void => {




    const now = Date.now()

    if (now - this.lastStdinTime > STDIN_RESUME_GAP_MS) {
      this.props.onStdinResume?.()
    }

    this.lastStdinTime = now

    try {
      let chunk

      while ((chunk = this.props.stdin.read() as string | null) !== null) {

        this.processInput(chunk)
      }
    } catch (error) {




      logError(error)




      const { stdin } = this.props

      if (this.rawModeEnabledCount > 0 && !stdin.listeners('readable').includes(this.handleReadable)) {
        logForDebugging('handleReadable: re-attaching stdin readable listener after error recovery', {
          level: 'warn'
        })
        stdin.addListener('readable', this.handleReadable)
      }
    }
  }
  handleInput = (input: string | undefined): void => {

    if (input === '\x03' && this.props.exitOnCtrlC) {
      this.handleExit()
    }

    // Note: Ctrl+Z (suspend) is now handled in processKeysInBatch using the
    // parsed key to support both raw (\x1a) and CSI u format from Kitty
    // keyboard protocol terminals (Ghostty, iTerm2, kitty, WezTerm)
  }
  handleExit = (error?: Error): void => {
    if (this.isRawModeSupported()) {
      this.handleSetRawMode(false)
    }

    this.props.onExit(error)
  }
  handleTerminalFocus = (isFocused: boolean): void => {


    setTerminalFocused(isFocused)
  }
  handleSuspend = (): void => {
    if (!this.isRawModeSupported()) {
      return
    }


    const rawModeCountBeforeSuspend = this.rawModeEnabledCount


    while (this.rawModeEnabledCount > 0) {
      this.handleSetRawMode(false)
    }






    if (this.props.stdout.isTTY) {
      this.props.stdout.write(SHOW_CURSOR + DFE + DISABLE_MOUSE_TRACKING)
    }

    this.inputEmitter.emit('suspend')


    const resumeHandler = () => {

      for (let i = 0; i < rawModeCountBeforeSuspend; i++) {
        if (this.isRawModeSupported()) {
          this.handleSetRawMode(true)
        }
      }

      if (this.props.stdout.isTTY) {
        this.props.stdout.write(EFE)
      }

      this.inputEmitter.emit('resume')
      process.removeListener('SIGCONT', resumeHandler)
    }

    process.on('SIGCONT', resumeHandler)
    process.kill(process.pid, 'SIGSTOP')
  }
}



function processKeysInBatch(app: App, items: ParsedInput[], _unused1: undefined, _unused2: undefined): void {






  if (
    items.some(i => i.kind === 'key' || (i.kind === 'mouse' && !((i.button & 0x20) !== 0 && (i.button & 0x03) === 3)))
  ) {
    updateLastInteractionTime()
  }

  for (const item of items) {


    if (item.kind === 'response') {
      app.querier.onResponse(item.response)

      continue
    }




    if (item.kind === 'mouse') {
      handleMouseEvent(app, item)

      continue
    }

    const sequence = item.sequence


    if (sequence === FOCUS_IN) {
      app.handleTerminalFocus(true)
      const event = new TerminalFocusEvent('terminalfocus')
      app.inputEmitter.emit('terminalfocus', event)

      continue
    }

    if (sequence === FOCUS_OUT) {
      app.handleTerminalFocus(false)






      if (app.props.selection.isDragging) {
        finishSelection(app.props.selection)
        app.props.onSelectionChange()
      }

      const event = new TerminalFocusEvent('terminalblur')
      app.inputEmitter.emit('terminalblur', event)

      continue
    }


    if (!getTerminalFocused()) {
      setTerminalFocused(true)
    }



    if (item.name === 'z' && item.ctrl && SUPPORTS_SUSPEND) {
      app.handleSuspend()

      continue
    }

    app.handleInput(sequence)
    const event = new InputEvent(item)
    app.inputEmitter.emit('input', event)


    app.props.dispatchKeyboardEvent(item)
  }
}


export function handleMouseEvent(app: App, m: ParsedMouse): void {
  const sel = app.props.selection

  const col = m.col - 1
  const row = m.row - 1
  const baseButton = m.button & 0x03


  if (isMouseClicksDisabled() && baseButton === 0) {
    return
  }

  if (m.action === 'press') {
    if ((m.button & 0x20) !== 0 && baseButton === 3) {
      if (app.mouseCaptureTarget) {
        app.props.onMouseUpAt(app.mouseCaptureTarget, col, row, baseButton)
        app.mouseCaptureTarget = undefined
      }










      if (sel.isDragging) {
        finishSelection(sel)
        app.props.onSelectionChange()
      }

      if (col === app.lastHoverCol && row === app.lastHoverRow) {
        return
      }

      app.lastHoverCol = col
      app.lastHoverRow = row
      app.props.onHoverAt(col, row)

      return
    }

    if (baseButton !== 0) {

      app.clickCount = 0
      app.props.onMouseDownAt(col, row, baseButton)

      return
    }

    if ((m.button & 0x20) !== 0) {
      if (app.mouseCaptureTarget) {
        app.props.onMouseDragAt(app.mouseCaptureTarget, col, row, baseButton)

        return
      }



      app.props.onSelectionDrag(col, row)

      return
    }






    if (sel.isDragging) {
      finishSelection(sel)
      app.props.onSelectionChange()
    }

    const capture = app.props.onMouseDownAt(col, row, baseButton)

    if (capture) {
      app.mouseCaptureTarget = capture
      app.clickCount = 0

      return
    }






    const now = Date.now()

    const nearLast =
      now - app.lastClickTime < MULTI_CLICK_TIMEOUT_MS &&
      Math.abs(col - app.lastClickCol) <= MULTI_CLICK_DISTANCE &&
      Math.abs(row - app.lastClickRow) <= MULTI_CLICK_DISTANCE

    app.clickCount = nearLast ? app.clickCount + 1 : 1
    app.lastClickTime = now
    app.lastClickCol = col
    app.lastClickRow = row

    if (app.clickCount >= 2) {


      if (app.pendingHyperlinkTimer) {
        clearTimeout(app.pendingHyperlinkTimer)
        app.pendingHyperlinkTimer = null
      }


      const count = app.clickCount === 2 ? 2 : 3
      app.props.onMultiClick(col, row, count)

      return
    }

    startSelection(sel, col, row)




    sel.lastPressHadAlt = (m.button & 0x08) !== 0
    app.props.onSelectionChange()

    return
  }







  if (app.mouseCaptureTarget) {
    app.props.onMouseUpAt(app.mouseCaptureTarget, col, row, baseButton)
    app.mouseCaptureTarget = undefined

    return
  }

  if (baseButton !== 0) {
    if (!sel.isDragging) {
      return
    }

    finishSelection(sel)
    app.props.onSelectionChange()

    return
  }

  finishSelection(sel)













  if (!hasSelection(sel) && sel.anchor) {



    if (!app.props.onClickAt(col, row)) {



      const url = app.props.getHyperlinkAt(col, row)












      if (url && process.env.TERM_PROGRAM !== 'vscode' && !isXtermJs()) {


        if (app.pendingHyperlinkTimer) {
          clearTimeout(app.pendingHyperlinkTimer)
        }

        app.pendingHyperlinkTimer = setTimeout(
          (app, url) => {
            app.pendingHyperlinkTimer = null
            app.props.onOpenHyperlink(url)
          },
          MULTI_CLICK_TIMEOUT_MS,
          app,
          url
        )
      }
    }
  }

  app.props.onSelectionChange()
}
