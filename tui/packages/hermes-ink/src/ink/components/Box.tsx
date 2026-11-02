





import '../global.d.ts'

import React, { type ReactNode, type Ref } from 'react'
import { c as _c } from 'react/compiler-runtime'
import type { Except } from 'type-fest'

import type { DOMElement } from '../dom.js'
import type { ClickEvent } from '../events/click-event.js'
import type { FocusEvent } from '../events/focus-event.js'
import type { KeyboardEvent } from '../events/keyboard-event.js'
import type { MouseEvent } from '../events/mouse-event.js'
import type { Styles } from '../styles.js'
import * as warn from '../warn.js'
export type Props = Except<Styles, 'textWrap'> & {
  children?: ReactNode
  ref?: Ref<DOMElement>




  tabIndex?: number





  autoFocus?: boolean






  onClick?: (event: ClickEvent) => void
  onMouseDown?: (event: MouseEvent) => void
  onMouseUp?: (event: MouseEvent) => void
  onMouseDrag?: (event: MouseEvent) => void
  onFocus?: (event: FocusEvent) => void
  onFocusCapture?: (event: FocusEvent) => void
  onBlur?: (event: FocusEvent) => void
  onBlurCapture?: (event: FocusEvent) => void
  onKeyDown?: (event: KeyboardEvent) => void
  onKeyDownCapture?: (event: KeyboardEvent) => void






  onMouseEnter?: () => void

  onMouseLeave?: () => void
}




function Box(t0: Props) {
  const $ = _c(48)
  let autoFocus
  let children
  let flexDirection
  let flexGrow
  let flexShrink
  let flexWrap
  let onBlur
  let onBlurCapture
  let onClick
  let onFocus
  let onFocusCapture
  let onKeyDown
  let onKeyDownCapture
  let onMouseDown
  let onMouseDrag
  let onMouseEnter
  let onMouseLeave
  let onMouseUp
  let ref
  let style
  let tabIndex

  if ($[0] !== t0) {
    const {
      children: t1,
      flexWrap: t2,
      flexDirection: t3,
      flexGrow: t4,
      flexShrink: t5,
      ref: t6,
      tabIndex: t7,
      autoFocus: t8,
      onClick: t9,
      onFocus: t10,
      onFocusCapture: t11,
      onBlur: t12,
      onBlurCapture: t13,
      onMouseDown: t14,
      onMouseUp: t15,
      onMouseDrag: t16,
      onMouseEnter: t17,
      onMouseLeave: t18,
      onKeyDown: t19,
      onKeyDownCapture: t20,
      ...t21
    } = t0

    children = t1
    ref = t6
    tabIndex = t7
    autoFocus = t8
    onClick = t9
    onFocus = t10
    onFocusCapture = t11
    onBlur = t12
    onBlurCapture = t13
    onMouseDown = t14
    onMouseUp = t15
    onMouseDrag = t16
    onMouseEnter = t17
    onMouseLeave = t18
    onKeyDown = t19
    onKeyDownCapture = t20
    style = t21
    flexWrap = t2 === undefined ? 'nowrap' : t2
    flexDirection = t3 === undefined ? 'row' : t3
    flexGrow = t4 === undefined ? 0 : t4
    flexShrink = t5 === undefined ? 1 : t5
    warn.ifNotInteger(style.margin, 'margin')
    warn.ifNotInteger(style.marginX, 'marginX')
    warn.ifNotInteger(style.marginY, 'marginY')
    warn.ifNotInteger(style.marginTop, 'marginTop')
    warn.ifNotInteger(style.marginBottom, 'marginBottom')
    warn.ifNotInteger(style.marginLeft, 'marginLeft')
    warn.ifNotInteger(style.marginRight, 'marginRight')
    warn.ifNotInteger(style.padding, 'padding')
    warn.ifNotInteger(style.paddingX, 'paddingX')
    warn.ifNotInteger(style.paddingY, 'paddingY')
    warn.ifNotInteger(style.paddingTop, 'paddingTop')
    warn.ifNotInteger(style.paddingBottom, 'paddingBottom')
    warn.ifNotInteger(style.paddingLeft, 'paddingLeft')
    warn.ifNotInteger(style.paddingRight, 'paddingRight')
    warn.ifNotInteger(style.gap, 'gap')
    warn.ifNotInteger(style.columnGap, 'columnGap')
    warn.ifNotInteger(style.rowGap, 'rowGap')
    $[0] = t0
    $[1] = autoFocus
    $[2] = children
    $[3] = flexDirection
    $[4] = flexGrow
    $[5] = flexShrink
    $[6] = flexWrap
    $[7] = onBlur
    $[8] = onBlurCapture
    $[9] = onClick
    $[10] = onFocus
    $[11] = onFocusCapture
    $[12] = onKeyDown
    $[13] = onKeyDownCapture
    $[14] = onMouseDown
    $[15] = onMouseUp
    $[16] = onMouseDrag
    $[17] = onMouseEnter
    $[18] = onMouseLeave
    $[19] = ref
    $[20] = style
    $[21] = tabIndex
  } else {
    autoFocus = $[1]
    children = $[2]
    flexDirection = $[3]
    flexGrow = $[4]
    flexShrink = $[5]
    flexWrap = $[6]
    onBlur = $[7]
    onBlurCapture = $[8]
    onClick = $[9]
    onFocus = $[10]
    onFocusCapture = $[11]
    onKeyDown = $[12]
    onKeyDownCapture = $[13]
    onMouseDown = $[14]
    onMouseUp = $[15]
    onMouseDrag = $[16]
    onMouseEnter = $[17]
    onMouseLeave = $[18]
    ref = $[19]
    style = $[20]
    tabIndex = $[21]
  }

  const t1 = style.overflowX ?? style.overflow ?? 'visible'
  const t2 = style.overflowY ?? style.overflow ?? 'visible'
  let t3

  if (
    $[22] !== flexDirection ||
    $[23] !== flexGrow ||
    $[24] !== flexShrink ||
    $[25] !== flexWrap ||
    $[26] !== style ||
    $[27] !== t1 ||
    $[28] !== t2
  ) {
    t3 = {
      flexWrap,
      flexDirection,
      flexGrow,
      flexShrink,
      ...style,
      overflowX: t1,
      overflowY: t2
    }
    $[22] = flexDirection
    $[23] = flexGrow
    $[24] = flexShrink
    $[25] = flexWrap
    $[26] = style
    $[27] = t1
    $[28] = t2
    $[29] = t3
  } else {
    t3 = $[29]
  }

  let t4

  if (
    $[30] !== autoFocus ||
    $[31] !== children ||
    $[32] !== onBlur ||
    $[33] !== onBlurCapture ||
    $[34] !== onClick ||
    $[35] !== onFocus ||
    $[36] !== onFocusCapture ||
    $[37] !== onKeyDown ||
    $[38] !== onKeyDownCapture ||
    $[39] !== onMouseDown ||
    $[40] !== onMouseUp ||
    $[41] !== onMouseDrag ||
    $[42] !== onMouseEnter ||
    $[43] !== onMouseLeave ||
    $[44] !== ref ||
    $[45] !== t3 ||
    $[46] !== tabIndex
  ) {
    t4 = (
      <ink-box
        autoFocus={autoFocus}
        onBlur={onBlur}
        onBlurCapture={onBlurCapture}
        onClick={onClick}
        onFocus={onFocus}
        onFocusCapture={onFocusCapture}
        onKeyDown={onKeyDown}
        onKeyDownCapture={onKeyDownCapture}
        onMouseDown={onMouseDown}
        onMouseDrag={onMouseDrag}
        onMouseEnter={onMouseEnter}
        onMouseLeave={onMouseLeave}
        onMouseUp={onMouseUp}
        ref={ref}
        style={t3}
        tabIndex={tabIndex}
      >
        {children}
      </ink-box>
    )
    $[30] = autoFocus
    $[31] = children
    $[32] = onBlur
    $[33] = onBlurCapture
    $[34] = onClick
    $[35] = onFocus
    $[36] = onFocusCapture
    $[37] = onKeyDown
    $[38] = onKeyDownCapture
    $[39] = onMouseDown
    $[40] = onMouseUp
    $[41] = onMouseDrag
    $[42] = onMouseEnter
    $[43] = onMouseLeave
    $[44] = ref
    $[45] = t3
    $[46] = tabIndex
    $[47] = t4
  } else {
    t4 = $[47]
  }

  return t4
}

export default Box
