




import { Box, Text } from '@hermes/ink'

import type { Theme } from '../theme.js'

import { clipToWidth } from '../lib/text.js'

export const QUEUE_WINDOW = 3

export function getQueueWindow(queueLen: number, queueEditIdx: number | null) {
  const start =
    queueEditIdx === null ? 0 : Math.max(0, Math.min(queueEditIdx - 1, Math.max(0, queueLen - QUEUE_WINDOW)))

  const end = Math.min(queueLen, start + QUEUE_WINDOW)

  return { end, showLead: start > 0, showTail: end < queueLen, start }
}

export function QueuedMessages({ cols, queueEditIdx, queued, t }: QueuedMessagesProps) {
  if (!queued.length) {
    return null
  }

  const q = getQueueWindow(queued.length, queueEditIdx)
  const room = Math.max(16, cols - 10)








  return (
    <Box flexDirection="column" marginTop={1}>
      {q.showLead && (
        <Text color={t.color.muted} dim>
          {'│ ⋮'}
        </Text>
      )}

      {queued.slice(q.start, q.end).map((item, i) => {
        const idx = q.start + i
        const active = queueEditIdx === idx

        return (
          <Text key={`${idx}-${item.slice(0, 16)}`} wrap="truncate-end">
            {/* The edited row swaps the rail for a caret and lights up. */}
            <Text color={active ? t.color.accent : t.color.muted} dim={!active}>
              {active ? '▸ ' : '│ '}
            </Text>
            {/* Queued text is the user's own words: full-weight so it stays
                readable against the dim rail. */}
            <Text color={active ? t.color.accent : t.color.text}>{clipToWidth(item, room)}</Text>
          </Text>
        )
      })}

      {q.showTail && (
        <Text color={t.color.muted} dim>
          {'│ …and '}
          {queued.length - q.end} more
        </Text>
      )}

      {queueEditIdx !== null && (
        <Text color={t.color.statusFg} dim>
          {'  Ctrl+X delete · Esc cancel'}
        </Text>
      )}
    </Box>
  )
}

interface QueuedMessagesProps {
  cols: number
  queueEditIdx: number | null
  queued: string[]
  t: Theme
}
