





import sliceAnsi from '../utils/sliceAnsi.js'

import { lruEvict } from './lru.js'
import { stringWidth } from './stringWidth.js'
import type { Styles } from './styles.js'
import { wrapAnsi } from './wrapAnsi.js'

const ELLIPSIS = '…'






const WRAP_CACHE_LIMIT = 4096
const wrapCache = new Map<string, string>()

function memoizedWrap(text: string, maxWidth: number, wrapType: Styles['textWrap']): string {



  const key = `${maxWidth}|${wrapType}|${text}`
  const cached = wrapCache.get(key)

  if (cached !== undefined) {

    wrapCache.delete(key)
    wrapCache.set(key, cached)

    return cached
  }

  const result = computeWrap(text, maxWidth, wrapType)

  if (wrapCache.size >= WRAP_CACHE_LIMIT) {
    wrapCache.delete(wrapCache.keys().next().value!)
  }

  wrapCache.set(key, result)

  return result
}



function sliceFit(text: string, start: number, end: number): string {
  const s = sliceAnsi(text, start, end)

  return stringWidth(s) > end - start ? sliceAnsi(text, start, end - 1) : s
}

function truncate(text: string, columns: number, position: 'start' | 'middle' | 'end'): string {
  if (columns < 1) {
    return ''
  }

  if (columns === 1) {
    return ELLIPSIS
  }

  const length = stringWidth(text)

  if (length <= columns) {
    return text
  }

  if (position === 'start') {
    return ELLIPSIS + sliceFit(text, length - columns + 1, length)
  }

  if (position === 'middle') {
    const half = Math.floor(columns / 2)

    return sliceFit(text, 0, half) + ELLIPSIS + sliceFit(text, length - (columns - half) + 1, length)
  }

  return sliceFit(text, 0, columns - 1) + ELLIPSIS
}

function trimSoftWrapBoundaries(text: string, maxWidth: number): string {
  return text
    .split('\n')
    .map(line => {
      const pieces = wrapAnsi(line, maxWidth, { trim: false, hard: true }).split('\n')

      if (pieces.length === 1) {
        return pieces[0]!
      }

      for (let index = 0; index < pieces.length - 1; index++) {
        const current = pieces[index]!
        const next = pieces[index + 1]!

        if (/\s$/.test(current)) {
          pieces[index] = current.replace(/\s$/, '')
        } else if (/^\s/.test(next)) {
          pieces[index + 1] = next.replace(/^\s/, '')
        }
      }

      return pieces.join('\n')
    })
    .join('\n')
}

function computeWrap(text: string, maxWidth: number, wrapType: Styles['textWrap']): string {
  if (wrapType === 'wrap') {
    return wrapAnsi(text, maxWidth, { trim: false, hard: true })
  }

  if (wrapType === 'wrap-char') {
    return wrapAnsi(text, maxWidth, { trim: false, hard: true, wordWrap: false })
  }

  if (wrapType === 'wrap-trim') {
    return trimSoftWrapBoundaries(text, maxWidth)
  }

  if (wrapType!.startsWith('truncate')) {
    const position: 'end' | 'middle' | 'start' =
      wrapType === 'truncate-middle' ? 'middle' : wrapType === 'truncate-start' ? 'start' : 'end'

    return truncate(text, maxWidth, position)
  }

  return text
}

export default function wrapText(text: string, maxWidth: number, wrapType: Styles['textWrap']): string {

  if (!text || maxWidth <= 0) {
    return computeWrap(text, maxWidth, wrapType)
  }

  return memoizedWrap(text, maxWidth, wrapType)
}

export function wrapCacheSize(): number {
  return wrapCache.size
}

export function evictWrapCache(keepRatio = 0): void {
  lruEvict(wrapCache, keepRatio)
}
