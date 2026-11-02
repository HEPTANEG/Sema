




import type { ScrollBoxHandle } from '@hermes/ink'

import {
  type RefObject,
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore
} from 'react'

const ESTIMATE = 4





const OVERSCAN = 20





const MAX_MOUNTED = 120
const COLD_START = 30




const PESSIMISTIC = 1





const QUANTUM = OVERSCAN >> 1




const FREEZE_RENDERS = 2







const SLIDE_STEP = 12

const NOOP = () => {}

const upperBound = (arr: ArrayLike<number>, target: number, length = arr.length) => {
  let lo = 0
  let hi = length

  while (lo < hi) {
    const mid = (lo + hi) >> 1

    arr[mid]! <= target ? (lo = mid + 1) : (hi = mid)
  }

  return lo
}

export const shouldSetVirtualClamp = ({
  itemCount,
  liveTailActive = false,
  sticky,
  viewportHeight
}: {
  itemCount: number
  liveTailActive?: boolean
  sticky: boolean
  viewportHeight: number
}) => itemCount > 0 && viewportHeight > 0 && !sticky && !liveTailActive

export const ensureVirtualItemHeight = (
  heights: Map<string, number>,
  key: string,
  index: number,
  estimate: number,
  estimateHeight?: (index: number, key: string) => number
) => {
  const cached = heights.get(key)

  if (cached !== undefined) {
    return Math.max(1, Math.floor(cached))
  }

  const seeded = Math.max(1, Math.floor(estimateHeight?.(index, key) ?? estimate))
  heights.set(key, seeded)

  return seeded
}

export function useVirtualHistory(
  scrollRef: RefObject<ScrollBoxHandle | null>,
  items: readonly { key: string }[],
  columns: number,
  {
    estimate = ESTIMATE,
    estimateHeight,
    initialHeights,
    liveTailActive = false,
    onHeightsChange,
    overscan = OVERSCAN,
    maxMounted = MAX_MOUNTED,
    coldStartCount = COLD_START
  }: VirtualHistoryOptions = {}
) {
  const nodes = useRef(new Map<string, unknown>())
  const heights = useRef(new Map(initialHeights))
  const initialHeightsRef = useRef(initialHeights)
  const refs = useRef(new Map<string, (el: unknown) => void>())
  const onHeightsChangeRef = useRef(onHeightsChange)


  const offsetVersion = useRef(0)





  const offsetsCache = useRef<{ arr: Float64Array; n: number; version: number }>({
    arr: new Float64Array(0),
    n: -1,
    version: -1
  })

  const [hasScrollRef, setHasScrollRef] = useState(false)


  const [measuredHeightVersion, bumpMeasuredHeightVersion] = useState(0)
  const metrics = useRef({ sticky: true, top: 0, vp: 0 })
  const lastScrollTopRef = useRef(0)







  const prevColumns = useRef(columns)
  const skipMeasurement = useRef(false)
  const prevRange = useRef<null | readonly [number, number]>(null)
  const freezeRenders = useRef(0)

  onHeightsChangeRef.current = onHeightsChange

  if (initialHeightsRef.current !== initialHeights) {
    initialHeightsRef.current = initialHeights
    heights.current = new Map(initialHeights)
    offsetVersion.current++
  }

  if (prevColumns.current !== columns && prevColumns.current > 0 && columns > 0) {
    const ratio = prevColumns.current / columns

    prevColumns.current = columns

    for (const [k, h] of heights.current) {
      heights.current.set(k, Math.max(1, Math.round(h * ratio)))
    }

    offsetVersion.current++
    skipMeasurement.current = true
    freezeRenders.current = FREEZE_RENDERS
  }

  useLayoutEffect(() => {
    setHasScrollRef(Boolean(scrollRef.current))
  }, [scrollRef])







  const subscribe = useCallback(
    (cb: () => void) => (hasScrollRef ? scrollRef.current?.subscribe(cb) : null) ?? NOOP,
    [hasScrollRef, scrollRef]
  )

  useSyncExternalStore(
    subscribe,
    () => {
      const s = scrollRef.current

      if (!s) {
        return NaN
      }

      const target = s.getScrollTop() + s.getPendingDelta()
      const bin = Math.floor(target / QUANTUM)

      return s.isSticky() ? ~bin : bin
    },
    () => NaN
  )

  useEffect(() => {
    const keep = new Set(items.map(i => i.key))
    let dirty = false

    for (const k of heights.current.keys()) {
      if (!keep.has(k)) {
        heights.current.delete(k)
        nodes.current.delete(k)
        refs.current.delete(k)
        dirty = true
      }
    }

    if (dirty) {
      offsetVersion.current++
    }
  }, [items])





  const n = items.length

  if (offsetsCache.current.version !== offsetVersion.current || offsetsCache.current.n !== n) {
    const arr = offsetsCache.current.arr.length >= n + 1 ? offsetsCache.current.arr : new Float64Array(n + 1)

    arr[0] = 0

    for (let i = 0; i < n; i++) {
      arr[i + 1] = arr[i]! + ensureVirtualItemHeight(heights.current, items[i]!.key, i, estimate, estimateHeight)
    }

    offsetsCache.current = { arr, n, version: offsetVersion.current }
  }

  const offsets = offsetsCache.current.arr
  const total = offsets[n] ?? 0
  const top = Math.max(0, scrollRef.current?.getScrollTop() ?? 0)
  const pendingDelta = scrollRef.current?.getPendingDelta() ?? 0
  const target = Math.max(0, top + pendingDelta)
  const vp = Math.max(0, scrollRef.current?.getViewportHeight() ?? 0)
  const sticky = scrollRef.current?.isSticky() ?? true
  const recentManual = Date.now() - (scrollRef.current?.getLastManualScrollAt() ?? 0) < 1200




  const frozenRange =
    freezeRenders.current > 0 && prevRange.current && prevRange.current[0] < n ? prevRange.current : null

  let start = 0
  let end = n

  if (frozenRange) {
    start = frozenRange[0]
    end = Math.min(frozenRange[1], n)
  } else if (n > 0) {
    if (vp <= 0) {
      start = Math.max(0, n - coldStartCount)
    } else if (sticky && !recentManual) {
      const budget = vp + overscan
      start = n

      while (start > 0 && total - offsets[start - 1]! < budget) {
        start--
      }
    } else {





      const MAX_SPAN = vp * 3
      const rawLo = Math.min(top, target)
      const rawHi = Math.max(top, target)
      const span = rawHi - rawLo
      const clampedLo = span > MAX_SPAN ? (pendingDelta < 0 ? rawHi - MAX_SPAN : rawLo) : rawLo
      const clampedHi = clampedLo + Math.min(span, MAX_SPAN)
      const lo = Math.max(0, clampedLo - overscan)
      const hi = clampedHi + vp + overscan



      start = Math.max(0, Math.min(n - 1, upperBound(offsets, lo, n + 1) - 1))
      end = Math.max(start + 1, Math.min(n, upperBound(offsets, hi, n + 1)))
    }
  }

  if (end - start > maxMounted) {
    sticky ? (start = Math.max(0, end - maxMounted)) : (end = Math.min(n, start + maxMounted))
  }





  if (n > 0 && vp > 0 && !frozenRange) {
    const needed = vp + 2 * overscan
    let coverage = 0

    for (let i = start; i < end; i++) {
      coverage += ensureVirtualItemHeight(heights.current, items[i]!.key, i, PESSIMISTIC, estimateHeight)
    }

    if (sticky) {
      const minStart = Math.max(0, end - maxMounted)

      while (start > minStart && coverage < needed) {
        start--
        coverage += ensureVirtualItemHeight(heights.current, items[start]!.key, start, PESSIMISTIC, estimateHeight)
      }
    } else {
      const maxEnd = Math.min(n, start + maxMounted)

      while (end < maxEnd && coverage < needed) {
        coverage += ensureVirtualItemHeight(heights.current, items[end]!.key, end, PESSIMISTIC, estimateHeight)
        end++
      }
    }
  }








  if (!frozenRange && prevRange.current && vp > 0) {
    const velocity = Math.abs(top - lastScrollTopRef.current) + Math.abs(pendingDelta)

    if (velocity > vp * 2) {
      const [pS, pE] = prevRange.current

      start = Math.max(start, pS - SLIDE_STEP)
      end = Math.min(end, pE + SLIDE_STEP)




      if (start > end) {
        end = Math.min(start + SLIDE_STEP, n)
      }
    }
  }

  lastScrollTopRef.current = top

  if (freezeRenders.current > 0) {
    freezeRenders.current--
  } else {
    prevRange.current = [start, end]
  }








  const dStart = useDeferredValue(start)
  const dEnd = useDeferredValue(end)
  let effStart = start < dStart ? dStart : start
  let effEnd = end > dEnd ? dEnd : end




  if (effStart > effEnd || sticky) {
    effStart = start
    effEnd = end
  }





  if (pendingDelta > 0) {
    effEnd = end
  }







  if (effEnd - effStart > maxMounted && vp > 0) {
    const mid = (offsets[effStart]! + offsets[effEnd]!) / 2

    if (top < mid) {
      effEnd = effStart + maxMounted
    } else {
      effStart = effEnd - maxMounted
    }
  }

  const measureRef = useCallback((key: string) => {
    let fn = refs.current.get(key)

    if (!fn) {
      fn = (el: unknown) => {
        if (el) {
          nodes.current.set(key, el)

          return
        }






        const existing = nodes.current.get(key) as MeasuredNode | undefined
        const h = Math.ceil(existing?.yogaNode?.getComputedHeight?.() ?? 0)

        if (h > 0 && heights.current.get(key) !== h) {
          heights.current.set(key, h)
          offsetVersion.current++
          onHeightsChangeRef.current?.(heights.current)
        }

        nodes.current.delete(key)
      }

      refs.current.set(key, fn)
    }

    return fn
  }, [])

  useLayoutEffect(() => {
    const s = scrollRef.current
    let dirty = false
    let heightDirty = false








    if (s && shouldSetVirtualClamp({ itemCount: n, liveTailActive, sticky, viewportHeight: vp })) {
      const effTopSpacer = offsets[effStart] ?? 0
      const effBottom = offsets[effEnd] ?? total






      const clampMin = effStart === 0 ? 0 : effTopSpacer
      const clampMax = effEnd === n ? Infinity : Math.max(effTopSpacer, effBottom - vp)

      s.setClampBounds(clampMin, clampMax)
    } else {
      s?.setClampBounds(undefined, undefined)
    }

    if (skipMeasurement.current) {
      skipMeasurement.current = false
    } else {
      for (let i = effStart; i < effEnd; i++) {
        const k = items[i]?.key

        if (!k) {
          continue
        }

        const h = Math.ceil((nodes.current.get(k) as MeasuredNode | undefined)?.yogaNode?.getComputedHeight?.() ?? 0)

        if (h > 0 && heights.current.get(k) !== h) {
          heights.current.set(k, h)
          dirty = true
          heightDirty = true
        }
      }
    }

    if (s) {
      const next = {
        sticky: s.isSticky(),
        top: Math.max(0, s.getScrollTop() + s.getPendingDelta()),
        vp: Math.max(0, s.getViewportHeight())
      }

      if (
        next.sticky !== metrics.current.sticky ||
        next.top !== metrics.current.top ||
        next.vp !== metrics.current.vp
      ) {
        metrics.current = next
        dirty = true
      }
    }

    if (dirty) {
      offsetVersion.current++
      onHeightsChangeRef.current?.(heights.current)
    }

    if (heightDirty) {
      bumpMeasuredHeightVersion(n => n + 1)
    }
  }, [effEnd, effStart, items, liveTailActive, measuredHeightVersion, n, offsets, scrollRef, sticky, total, vp])

  return {
    bottomSpacer: Math.max(0, total - (offsets[effEnd] ?? total)),
    end: effEnd,
    measureRef,
    offsets,
    start: effStart,
    topSpacer: offsets[effStart] ?? 0
  }
}

interface MeasuredNode {
  yogaNode?: { getComputedHeight?: () => number } | null
}

interface VirtualHistoryOptions {
  coldStartCount?: number
  estimate?: number
  estimateHeight?: (index: number, key: string) => number
  initialHeights?: ReadonlyMap<string, number>
  liveTailActive?: boolean
  maxMounted?: number
  onHeightsChange?: (heights: ReadonlyMap<string, number>) => void
  overscan?: number
}
