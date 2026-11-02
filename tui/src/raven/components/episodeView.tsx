



import { Box, NoSelect, stringWidth, Text } from '@hermes/ink'
import { memo, useEffect, useState } from 'react'

import type { Theme } from '../theme.js'
import type { Episode, EpisodeTool, Msg } from '../types.js'

import { episodeFailed, groupTools, toolParts, toolsSummary } from '../domain/episodeSummary.js'
import { fmtDuration } from '../domain/messages.js'
import { hasMeaningfulReasoning } from '../lib/reasoning.js'
import { boundedLiveRenderText, clipToWidth, compactPreview, tailPreview } from '../lib/text.js'
import { Md } from './markdown.js'
import { StreamingMd } from './streamingMarkdown.js'
import { Spinner } from './thinking.js'





const ROW_SLACK = 4



const useNow = (active: boolean) => {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active) {
      return
    }

    const id = setInterval(() => setNow(Date.now()), 1000)

    return () => clearInterval(id)
  }, [active])

  return now
}




const toolElapsed = (tool: EpisodeTool, now: number): number | undefined =>
  tool.durationMs ?? (!tool.done && tool.startedAt ? Math.max(0, now - tool.startedAt) : undefined)



const ToolRow = memo(function ToolRow({
  compact,
  isOpen,
  live,
  now,
  onToggle,
  t,
  tool,
  width
}: {
  compact?: boolean
  isOpen: boolean
  live: boolean
  now: number
  onToggle: () => void
  t: Theme
  tool: EpisodeTool
  width?: number
}) {
  const toolRunning = live && !tool.done
  const parts = toolParts(tool)
  const elapsed = toolElapsed(tool, now)



  const room = (width ?? 120) - ROW_SLACK
  const suffixW = (elapsed != null ? fmtDuration(elapsed).length + (toolRunning ? 4 : 3) : 0) + (tool.ok ? 0 : 2)
  const detail = parts.detail
    ? clipToWidth(parts.detail, Math.max(6, room - 2 - stringWidth(parts.verb) - 3 - suffixW))
    : ''
  const resultLines = (tool.resultPreview ?? '')
    .split('\n')
    .map(l => l.trim())
    .filter(Boolean)

  return (
    <Box flexDirection="column">
      <Box>
        <NoSelect fromLeftEdge onClick={tool.diff ? onToggle : undefined}>
          {toolRunning ? (
            <Text>
              <Spinner color={t.color.accent} variant="tool" />{' '}
            </Text>
          ) : (
            <Text color={t.color.accent}>{tool.diff ? (isOpen ? '▾ ' : '▸ ') : '· '}</Text>
          )}
        </NoSelect>
        <Box flexShrink={1} minWidth={0}>
          <Text wrap="truncate-end">
            <Text bold color={tool.ok ? t.color.accent : t.color.error}>
              {parts.verb}
            </Text>
            {detail ? (
              <Text color={t.color.statusFg} dim>
                {' ('}
                {detail}
                {')'}
              </Text>
            ) : (
              ''
            )}
          </Text>
        </Box>
        {elapsed != null ? (
          <NoSelect flexShrink={0}>
            <Text color={t.color.statusFg} dim>
              {' ('}
              {fmtDuration(elapsed)}
              {toolRunning ? '…' : ''}
              {')'}
            </Text>
          </NoSelect>
        ) : null}
        {tool.ok ? null : (
          <NoSelect flexShrink={0}>
            <Text color={t.color.error}> ✗</Text>
          </NoSelect>
        )}
      </Box>

      {/* Result preview is shown inline (info density); only a large diff is
          click-to-expand. A tool may report several lines (ask_user's
          question -> answer pairs); each becomes its own row. */}
      {resultLines.map((line, i) => (
        <Box key={i}>
          <NoSelect fromLeftEdge>
            <Text color={t.color.muted} dim>
              {'  '}
              {i === resultLines.length - 1 ? '└ ' : '├ '}
            </Text>
          </NoSelect>
          <Box flexGrow={1} minWidth={0}>
            <Text color={t.color.muted} dim wrap="truncate-end">
              {clipToWidth(line, Math.max(8, room - 4))}
            </Text>
          </Box>
        </Box>
      ))}

      {isOpen && tool.diff ? <Md compact={compact} t={t} text={`\`\`\`diff\n${tool.diff}\n\`\`\``} /> : null}
    </Box>
  )
})




const ToolGroup = memo(function ToolGroup({
  live,
  now,
  t,
  tools,
  width
}: {
  live: boolean
  now: number
  t: Theme
  tools: EpisodeTool[]
  width?: number
}) {
  const verb = toolParts(tools[0]!).verb
  const anyFailed = tools.some(tool => !tool.ok)

  return (
    <Box flexDirection="column">
      <Box>
        <NoSelect fromLeftEdge>
          <Text color={t.color.accent}>{'· '}</Text>
        </NoSelect>
        <Text>
          <Text bold color={anyFailed ? t.color.error : t.color.accent}>
            {verb}
          </Text>
          <Text color={t.color.statusFg} dim>
            {' ('}
            {tools.length}
            {')'}
          </Text>
        </Text>
      </Box>

      {tools.map((tool, i) => {
        const last = i === tools.length - 1
        const toolRunning = live && !tool.done
        const elapsed = toolElapsed(tool, now)
        const suffixW = (elapsed != null ? fmtDuration(elapsed).length + (toolRunning ? 4 : 3) : 0) + (tool.ok ? 0 : 2)

        const detail = clipToWidth(
          toolParts(tool).detail || toolParts(tool).verb,
          Math.max(6, (width ?? 120) - ROW_SLACK - 4 - suffixW)
        )

        return (
          <Box flexDirection="column" key={tool.id}>
            <Box>
              <NoSelect fromLeftEdge>
                {toolRunning ? (
                  <Text>
                    {'  '}
                    <Spinner color={t.color.accent} variant="tool" />{' '}
                  </Text>
                ) : (
                  <Text color={t.color.accent} dim>
                    {'  '}
                    {last ? '└ ' : '├ '}
                  </Text>
                )}
              </NoSelect>
              <Box flexShrink={1} minWidth={0}>
                <Text color={tool.ok ? t.color.muted : t.color.error} dim={tool.ok} wrap="truncate-end">
                  {detail}
                </Text>
              </Box>
              {elapsed != null ? (
                <NoSelect flexShrink={0}>
                  <Text color={t.color.statusFg} dim>
                    {' ('}
                    {fmtDuration(elapsed)}
                    {toolRunning ? '…' : ''}
                    {')'}
                  </Text>
                </NoSelect>
              ) : null}
              {tool.ok ? null : (
                <NoSelect flexShrink={0}>
                  <Text color={t.color.error}> ✗</Text>
                </NoSelect>
              )}
            </Box>

            {tool.resultPreview ? (
              <Box>
                <NoSelect fromLeftEdge>
                  <Text color={t.color.muted} dim>
                    {'  '}
                    {last ? '  ' : '│ '}
                    {'└ '}
                  </Text>
                </NoSelect>
                <Box flexGrow={1} minWidth={0}>
                  <Text color={t.color.muted} dim wrap="truncate-end">
                    {clipToWidth(tool.resultPreview, Math.max(8, (width ?? 120) - ROW_SLACK - 6))}
                  </Text>
                </Box>
              </Box>
            ) : null}
          </Box>
        )
      })}
    </Box>
  )
})








export const EpisodeView = memo(function EpisodeView({
  cols,
  compact,
  episodes,
  live = false,
  t,
  text
}: {
  cols?: number
  compact?: boolean
  episodes: Episode[]
  live?: boolean
  t: Theme
  text?: string
}) {


  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set())
  const toggle = (key: string) =>
    setOpen(prev => {
      const next = new Set(prev)

      if (!next.delete(key)) {
        next.add(key)
      }

      return next
    })

  const lastIdx = episodes.length - 1

  const now = useNow(live && episodes.length > 0)

  const reasoningText = (ms?: number) => (ms ? `reasoning for ${fmtDuration(ms)}` : 'reasoning')






  const width = cols ? Math.max(20, cols - 4) : undefined




  const PROSE_INDENT = 2
  const proseWidth = width ? Math.max(20, width - PROSE_INDENT) : undefined

  return (
    <Box flexDirection="column" width={width}>
      {episodes.map((ep, epIdx) => {



        const prev = epIdx > 0 ? episodes[epIdx - 1] : undefined
        const prevShowedTools = Boolean(
          prev &&
          prev.tools.length > 0 &&
          ((prev.narration ?? '').trim() || (live && prev.index === episodes[lastIdx]?.index))
        )
        const running = live && ep.index === episodes[lastIdx]?.index
        const reasoning = (ep.reasoning ?? '').trim()
        const hasReasoning = hasMeaningfulReasoning(reasoning)



        const narration = (ep.narration ?? '').trim().replace(/^[\s，,、；;：:。.]+/, '')
        const hasNarration = narration.length > 0




        const thinking = running && !hasNarration && ep.tools.length === 0 && !text
        const reasoningMs =
          ep.reasoningMs ?? ep.durationMs ?? (thinking && ep.startedAt ? Math.max(0, now - ep.startedAt) : undefined)



        const toolRows = groupTools(ep.tools).map(g =>
          g.length === 1 ? (
            <ToolRow
              compact={compact}
              isOpen={open.has(`tool:${g[0]!.id}`)}
              key={g[0]!.id}
              live={live}
              now={now}
              onToggle={() => toggle(`tool:${g[0]!.id}`)}
              t={t}
              tool={g[0]!}
              width={width}
            />
          ) : (
            <ToolGroup key={g[0]!.id} live={live} now={now} t={t} tools={g} width={width} />
          )
        )



        const toolBlock = ep.tools.length ? (
          <Box flexDirection="column" marginTop={1}>
            {toolRows}
          </Box>
        ) : null



        const cot = (tail: boolean) =>
          hasReasoning ? (
            <Box paddingLeft={2}>
              <Text color={t.color.muted} dim wrap="wrap-trim">
                {(tail ? tailPreview : compactPreview)(reasoning, 4000)}
              </Text>
            </Box>
          ) : null





        if (!hasNarration && !running) {
          const epOpen = open.has(`ep:${ep.index}`)
          const bits = [hasReasoning ? reasoningText(reasoningMs) : '', toolsSummary(ep.tools)]
            .filter(Boolean)
            .join(' · ')

          return (
            <Box flexDirection="column" key={ep.index} marginTop={prevShowedTools ? 1 : 0}>
              <Box>
                <NoSelect fromLeftEdge onClick={() => toggle(`ep:${ep.index}`)}>
                  <Text color={t.color.accent}>{epOpen ? '▾ ' : '▸ '}</Text>
                </NoSelect>
                <Box flexGrow={1} minWidth={0}>
                  <Text color={episodeFailed(ep) ? t.color.error : t.color.muted} dim wrap="truncate-end">
                    {bits ? clipToWidth(bits, Math.max(8, (width ?? 120) - ROW_SLACK - 2)) : '…'}
                  </Text>
                </Box>
              </Box>
              {epOpen ? (
                <>
                  {cot(false)}
                  {toolBlock}
                </>
              ) : null}
            </Box>
          )
        }






        const liveReasoning = thinking && hasReasoning
        const rsnOpen = liveReasoning || open.has(`rsn:${ep.index}`)

        return (
          <Box flexDirection="column" key={ep.index} marginTop={prevShowedTools ? 1 : 0}>
            {hasReasoning ? (
              <Box flexDirection="column">
                <Box>
                  <NoSelect fromLeftEdge onClick={() => toggle(`rsn:${ep.index}`)}>
                    <Text color={t.color.accent}>{rsnOpen ? '▾ ' : '▸ '}</Text>
                  </NoSelect>
                  <Text color={t.color.muted} dim>
                    {reasoningText(reasoningMs)}
                  </Text>
                </Box>
                {rsnOpen ? cot(liveReasoning || running) : null}
              </Box>
            ) : null}

            {hasNarration || (running && text) ? (
              <Box marginTop={hasReasoning ? 1 : 0} paddingLeft={PROSE_INDENT}>
                {hasNarration ? (
                  <Md avail={proseWidth} compact={compact} t={t} text={narration} />
                ) : (
                  <StreamingMd compact={compact} t={t} text={boundedLiveRenderText(text ?? '')} />
                )}
              </Box>
            ) : null}

            {toolBlock}
          </Box>
        )
      })}

      {text && (!live || episodes.length === 0) ? (
        <Box marginTop={episodes.length > 0 ? 1 : 0} paddingLeft={PROSE_INDENT}>
          <Md avail={proseWidth} compact={compact} t={t} text={text} />
        </Box>
      ) : null}
    </Box>
  )
})


export const EpisodeMessage = memo(function EpisodeMessage({
  cols,
  compact,
  msg,
  t
}: {
  cols?: number
  compact?: boolean
  msg: Msg
  t: Theme
}) {
  return <EpisodeView cols={cols} compact={compact} episodes={msg.episodes ?? []} t={t} text={msg.text} />
})
