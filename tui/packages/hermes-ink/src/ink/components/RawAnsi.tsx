





import React from 'react'
import { c as _c } from 'react/compiler-runtime'
type Props = {




  lines: string[]

  width: number
}
















export function RawAnsi(t0: Props) {
  const $ = _c(6)

  const { lines, width } = t0

  if (lines.length === 0) {
    return null
  }

  let t1

  if ($[0] !== lines) {
    t1 = lines.join('\n')
    $[0] = lines
    $[1] = t1
  } else {
    t1 = $[1]
  }

  let t2

  if ($[2] !== lines.length || $[3] !== t1 || $[4] !== width) {
    t2 = <ink-raw-ansi rawHeight={lines.length} rawText={t1} rawWidth={width} />
    $[2] = lines.length
    $[3] = t1
    $[4] = width
    $[5] = t2
  } else {
    t2 = $[5]
  }

  return t2
}
