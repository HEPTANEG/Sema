




import { Box, Link, stringWidth, Text } from '@hermes/ink'
import { Fragment, memo, type ReactNode, useMemo } from 'react'

import type { Theme } from '../theme.js'

import { ensureEmojiPresentation } from '../lib/emoji.js'
import { normalizeExternalUrl, urlSlugTitleLabel, useLinkTitle } from '../lib/externalLink.js'
import { BOX_CLOSE, BOX_OPEN, texToUnicode } from '../lib/mathUnicode.js'
import { highlightLine, isHighlightable } from '../lib/syntax.js'








const renderMath = (text: string): ReactNode => {
  if (!text.includes(BOX_OPEN)) {
    return text
  }

  const out: ReactNode[] = []
  let i = 0
  let key = 0

  while (i < text.length) {
    const start = text.indexOf(BOX_OPEN, i)

    if (start < 0) {
      out.push(text.slice(i))

      break
    }

    if (start > i) {
      out.push(text.slice(i, start))
    }

    const end = text.indexOf(BOX_CLOSE, start + 1)

    if (end < 0) {
      out.push(text.slice(start))

      break
    }

    out.push(
      <Text bold inverse key={key++}>
        {' '}
        {text.slice(start + 1, end)}{' '}
      </Text>
    )

    i = end + 1
  }

  return out
}

const FENCE_RE = /^\s*(`{3,}|~{3,})(.*)$/
const FENCE_CLOSE_RE = /^\s*(`{3,}|~{3,})\s*$/
const HR_RE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+\s*)?$/
const SETEXT_RE = /^\s{0,3}(=+|-+)\s*$/
const FOOTNOTE_RE = /^\[\^([^\]]+)\]:\s*(.*)$/
const DEF_RE = /^\s*:\s+(.+)$/
const BULLET_RE = /^(\s*)[-+*]\s+(.*)$/
const TASK_RE = /^\[( |x|X)\]\s+(.*)$/
const NUMBERED_RE = /^(\s*)(\d+)[.)]\s+(.*)$/
const QUOTE_RE = /^\s*(?:>\s*)+/
const TABLE_DIVIDER_CELL_RE = /^:?-{3,}:?$/
const MD_URL_RE = '((?:[^\\s()]|\\([^\\s()]*\\))+?)'






const MATH_BLOCK_OPEN_RE = /^\s*(\$\$|\\\[)(.*)$/
const MATH_BLOCK_CLOSE_DOLLAR_RE = /^(.*?)\$\$\s*$/
const MATH_BLOCK_CLOSE_BRACKET_RE = /^(.*?)\\\]\s*$/

export const MEDIA_LINE_RE = /^\s*[`"']?MEDIA:\s*(\S+?)[`"']?\s*$/
export const AUDIO_DIRECTIVE_RE = /^\s*\[\[audio_as_voice\]\]\s*$/

















export const INLINE_RE = new RegExp(
  [
    `!\\[(.*?)\\]\\(${MD_URL_RE}\\)`, // 1,2  image
    `\\[(.+?)\\]\\(${MD_URL_RE}\\)`, // 3,4  link
    `<((?:https?:\\/\\/|mailto:)[^>\\s]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,})>`, // 5   autolink
    `~~(.+?)~~`, // 6    strike
    `\`([^\\\`]+)\``, // 7    code
    `\\*\\*(.+?)\\*\\*`, // 8    bold *
    `(?<!\\w)__(.+?)__(?!\\w)`, // 9    bold _
    `\\*(.+?)\\*`, // 10   italic *
    `(?<!\\w)_(.+?)_(?!\\w)`, // 11   italic _
    `==(.+?)==`, // 12   highlight
    `\\[\\^([^\\]]+)\\]`, // 13   footnote ref
    `\\^([^^\\s][^^]*?)\\^`, // 14   superscript
    `~([A-Za-z0-9]{1,8})~`, // 15   subscript
    `(https?:\\/\\/[^\\s<]+)`, // 16   bare URL — wrapped so it owns its own




    `(?<!\\$)\\$([^\\s$](?:[^$\\n]*?[^\\s$])?)\\$(?!\\$)`, // 17   inline math $...$
    `\\\\\\(([^\\n]+?)\\\\\\)`
  ].join('|'),
  'g'
)

const indentDepth = (s: string) => Math.floor(s.replace(/\t/g, '  ').length / 2)

const splitRow = (row: string) =>
  row
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map(c => c.trim())

const isTableDivider = (row: string) => {
  const cells = splitRow(row)

  return cells.length > 1 && cells.every(c => TABLE_DIVIDER_CELL_RE.test(c))
}

const autolinkUrl = (raw: string) =>
  raw.startsWith('mailto:') || raw.startsWith('http') || !raw.includes('@') ? raw : `mailto:${raw}`

const defaultLinkLabel = (url: string) =>
  url.startsWith('mailto:') ? url.replace(/^mailto:/, '') : /^https?:\/\//i.test(url) ? urlSlugTitleLabel(url) : url

const pickFallbackLabel = (label: string | undefined, target: string): string | undefined => {
  const trimmed = label?.trim()

  if (!trimmed) {
    return undefined
  }

  return normalizeExternalUrl(trimmed) === target ? undefined : trimmed
}

interface ResolvedLinkProps {
  fallbackLabel?: string
  t: Theme
  url: string
}

function ResolvedLink({ fallbackLabel, t, url }: ResolvedLinkProps) {
  const fetched = useLinkTitle(url)
  const display = fetched || fallbackLabel || defaultLinkLabel(url)

  return (
    <Link url={url}>
      <Text color={t.color.accent} underline>
        {display}
      </Text>
    </Link>
  )
}

const renderResolvedLink = (k: number, t: Theme, rawUrl: string, label?: string) => {
  const target = normalizeExternalUrl(rawUrl)

  return <ResolvedLink fallbackLabel={pickFallbackLabel(label, target)} key={k} t={t} url={target} />
}

export const stripInlineMarkup = (v: string) =>
  v
    .replace(/!\[(.*?)\]\(((?:[^\s()]|\([^\s()]*\))+?)\)/g, '[image: $1] $2')
    .replace(/\[(.+?)\]\(((?:[^\s()]|\([^\s()]*\))+?)\)/g, '$1')
    .replace(/<((?:https?:\/\/|mailto:)[^>\s]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>/g, '$1')
    .replace(/~~(.+?)~~/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/(?<!\w)__(.+?)__(?!\w)/g, '$1')
    .replace(/\*(.+?)\*/g, '$1')
    .replace(/(?<!\w)_(.+?)_(?!\w)/g, '$1')
    .replace(/==(.+?)==/g, '$1')
    .replace(/\[\^([^\]]+)\]/g, '[$1]')
    .replace(/\^([^^\s][^^]*?)\^/g, '^$1')
    .replace(/~([A-Za-z0-9]{1,8})~/g, '_$1')
    .replace(/(?<!\$)\$([^\s$](?:[^$\n]*?[^\s$])?)\$(?!\$)/g, '$1')
    .replace(/\\\(([^\n]+?)\\\)/g, '$1')











const clipWidth = (raw: string, width: number): string => {
  const text = raw.replace(/\s+/g, ' ').trim()

  if (width <= 0 || stringWidth(text) <= width) {
    return text
  }

  let out = ''
  let w = 0

  for (const ch of text) {
    const cw = stringWidth(ch)

    if (w + cw > width - 1) {
      break
    }

    out += ch
    w += cw
  }

  return `${out}…`
}




const fitWidths = (content: number[], budget: number): number[] => {
  const total = content.reduce((a, b) => a + b, 0)

  if (total <= budget) {
    return content
  }

  const min = 6
  const scale = budget / total
  const widths = content.map(c => Math.max(min, Math.floor(c * scale)))

  let over = widths.reduce((a, b) => a + b, 0) - budget

  while (over > 0) {
    const widest = widths.indexOf(Math.max(...widths))

    if (widths[widest]! <= min) {
      break
    }

    widths[widest] = widths[widest]! - 1
    over--
  }

  return widths
}

const renderTable = (k: number, rows: string[][], t: Theme, avail?: number) => {






  const cellWidth = (raw: string) => stringWidth(stripInlineMarkup(raw))

  const GAP = 2
  const content = rows[0]!.map((_, ci) => Math.max(...rows.map(r => cellWidth(r[ci] ?? ''))))

  const room = (avail ?? process.stdout.columns ?? 80) - 8 - GAP * (content.length - 1)
  const widths = fitWidths(content, Math.max(content.length * 6, room))












  const cells = (render: (w: number, ci: number) => ReactNode) =>
    widths.map((w, ci) => (
      <Fragment key={ci}>
        {ci > 0 ? <Box flexShrink={0} width={GAP} /> : null}
        <Box flexShrink={0} width={w}>
          {render(w, ci)}
        </Box>
      </Fragment>
    ))

  return (
    <Box flexDirection="column" key={k} paddingLeft={2}>
      {rows.map((row, ri) => (
        <Fragment key={ri}>
          <Box>
            {/* Cells are pre-wrapped to their own column width, so inline
                markup is stripped rather than rendered via MdInline: a nested
                <Text> would re-wrap on spaces and undo the CJK-aware wrap. */}
            {cells((w, ci) => (
              <Text bold={ri === 0} color={ri === 0 ? t.color.accent : undefined}>
                {clipWidth(stripInlineMarkup(row[ci] ?? ''), w)}
              </Text>
            ))}
          </Box>
          {ri === 0 && rows.length > 1 ? (
            <Box>
              {cells(w => (
                <Text color={t.color.muted} dimColor wrap="truncate-end">
                  {'─'.repeat(Math.max(1, w))}
                </Text>
              ))}
            </Box>
          ) : null}
        </Fragment>
      ))}
    </Box>
  )
}

function MdInline({ t, text }: { t: Theme; text: string }) {
  const parts: ReactNode[] = []

  let last = 0

  for (const m of text.matchAll(INLINE_RE)) {
    const i = m.index ?? 0
    const k = parts.length

    if (i > last) {
      parts.push(<Text key={k}>{text.slice(last, i)}</Text>)
    }

    if (m[1] && m[2]) {
      parts.push(
        <Text color={t.color.muted} key={parts.length}>
          [image: {m[1]}] {m[2]}
        </Text>
      )
    } else if (m[3] && m[4]) {
      parts.push(renderResolvedLink(parts.length, t, m[4], m[3]))
    } else if (m[5]) {
      parts.push(renderResolvedLink(parts.length, t, autolinkUrl(m[5]), m[5].replace(/^mailto:/, '')))
    } else if (m[6]) {
      parts.push(
        <Text key={parts.length} strikethrough>
          <MdInline t={t} text={m[6]} />
        </Text>
      )
    } else if (m[7]) {



      parts.push(
        <Text color={t.color.accent} dimColor key={parts.length}>
          {m[7]}
        </Text>
      )
    } else if (m[8] ?? m[9]) {





      parts.push(
        <Text bold key={parts.length}>
          <MdInline t={t} text={m[8] ?? m[9]!} />
        </Text>
      )
    } else if (m[10] ?? m[11]) {
      parts.push(
        <Text italic key={parts.length}>
          <MdInline t={t} text={m[10] ?? m[11]!} />
        </Text>
      )
    } else if (m[12]) {
      parts.push(
        <Text backgroundColor={t.color.diffAdded} color={t.color.diffAddedWord} key={parts.length}>
          <MdInline t={t} text={m[12]} />
        </Text>
      )
    } else if (m[13]) {
      parts.push(
        <Text color={t.color.muted} key={parts.length}>
          [{m[13]}]
        </Text>
      )
    } else if (m[14]) {
      parts.push(
        <Text color={t.color.muted} key={parts.length}>
          ^{m[14]}
        </Text>
      )
    } else if (m[15]) {
      parts.push(
        <Text color={t.color.muted} key={parts.length}>
          _{m[15]}
        </Text>
      )
    } else if (m[16]) {


      const url = m[16].replace(/[),.;:!?]+$/g, '')

      parts.push(renderResolvedLink(parts.length, t, url))

      if (url.length < m[16].length) {
        parts.push(<Text key={parts.length}>{m[16].slice(url.length)}</Text>)
      }
    } else if (m[17] ?? m[18]) {







      parts.push(
        <Text color={t.color.accent} italic key={parts.length}>
          {renderMath(texToUnicode(m[17] ?? m[18]!))}
        </Text>
      )
    }

    last = i + m[0].length
  }

  if (last < text.length) {
    parts.push(<Text key={parts.length}>{text.slice(last)}</Text>)
  }

  return <Text wrap="wrap-trim">{parts.length ? parts : text}</Text>
}




const MD_CACHE_LIMIT = 512
const mdCache = new WeakMap<Theme, Map<string, ReactNode[]>>()

const cacheBucket = (t: Theme) => {
  const b = mdCache.get(t)

  if (b) {
    return b
  }

  const fresh = new Map<string, ReactNode[]>()
  mdCache.set(t, fresh)

  return fresh
}

const cacheGet = (b: Map<string, ReactNode[]>, key: string) => {
  const v = b.get(key)

  if (v) {
    b.delete(key)
    b.set(key, v)
  }

  return v
}

const cacheSet = (b: Map<string, ReactNode[]>, key: string, v: ReactNode[]) => {
  b.set(key, v)

  if (b.size > MD_CACHE_LIMIT) {
    b.delete(b.keys().next().value!)
  }
}

function MdImpl({ avail, compact, t, text }: MdProps) {
  const nodes = useMemo(() => {
    const bucket = cacheBucket(t)
    const cacheKey = `${compact ? '1' : '0'}|${avail ?? 0}|${text}`
    const cached = cacheGet(bucket, cacheKey)

    if (cached) {
      return cached
    }

    const lines = ensureEmojiPresentation(text).split('\n')
    const nodes: ReactNode[] = []

    let prevKind: Kind = null
    let i = 0

    const gap = () => {
      if (nodes.length && prevKind !== 'blank') {
        nodes.push(<Text key={`gap-${nodes.length}`}> </Text>)
        prevKind = 'blank'
      }
    }

    const start = (kind: Exclude<Kind, null | 'blank'>) => {
      if (prevKind && prevKind !== 'blank' && prevKind !== kind) {
        gap()
      }

      prevKind = kind
    }

    while (i < lines.length) {
      const line = lines[i]!
      const key = nodes.length

      if (!line.trim()) {
        if (!compact) {
          gap()
        }

        i++

        continue
      }

      if (AUDIO_DIRECTIVE_RE.test(line)) {
        i++

        continue
      }

      const media = line.match(MEDIA_LINE_RE)?.[1]

      if (media) {
        start('paragraph')
        nodes.push(
          <Text color={t.color.muted} key={key} wrap="wrap-trim">
            {'▸ '}

            <Link url={/^(?:\/|[a-z]:[\\/])/i.test(media) ? `file://${media}` : media}>
              <Text color={t.color.accent} underline>
                {media}
              </Text>
            </Link>
          </Text>
        )
        i++

        continue
      }

      const fence = line.match(FENCE_RE)

      if (fence) {
        const char = fence[1]![0] as '`' | '~'
        const len = fence[1]!.length
        const lang = fence[2]!.trim().toLowerCase()
        const block: string[] = []

        for (i++; i < lines.length; i++) {
          const close = lines[i]!.match(FENCE_CLOSE_RE)?.[1]

          if (close && close[0] === char && close.length >= len) {
            break
          }

          block.push(lines[i]!)
        }

        if (i < lines.length) {
          i++
        }

        if (['md', 'markdown'].includes(lang)) {
          start('paragraph')
          nodes.push(<Md compact={compact} key={key} t={t} text={block.join('\n')} />)

          continue
        }

        start('code')

        const isDiff = lang === 'diff'
        const highlighted = !isDiff && isHighlightable(lang)

        nodes.push(
          <Box flexDirection="column" key={key} paddingLeft={2}>
            {lang && !isDiff && <Text color={t.color.muted}>{'─ ' + lang}</Text>}

            {block.map((l, j) => {
              if (highlighted) {
                return (
                  <Text key={j}>
                    {highlightLine(l, lang, t).map(([color, text], kk) =>
                      color ? (
                        <Text color={color} key={kk}>
                          {text}
                        </Text>
                      ) : (
                        <Text key={kk}>{text}</Text>
                      )
                    )}
                  </Text>
                )
              }

              const add = isDiff && l.startsWith('+')
              const del = isDiff && l.startsWith('-')
              const hunk = isDiff && l.startsWith('@@')

              return (
                <Text
                  backgroundColor={add ? t.color.diffAdded : del ? t.color.diffRemoved : undefined}
                  color={add ? t.color.diffAddedWord : del ? t.color.diffRemovedWord : hunk ? t.color.muted : undefined}
                  dimColor={isDiff && !add && !del && !hunk && l.startsWith(' ')}
                  key={j}
                >
                  {l}
                </Text>
              )
            })}
          </Box>
        )

        continue
      }

      const mathOpen = line.match(MATH_BLOCK_OPEN_RE)

      if (mathOpen) {
        const opener = mathOpen[1]!
        const closeRe = opener === '$$' ? MATH_BLOCK_CLOSE_DOLLAR_RE : MATH_BLOCK_CLOSE_BRACKET_RE
        const headRest = mathOpen[2] ?? ''
        const block: string[] = []





        const sameLineClose = headRest.match(closeRe)

        if (sameLineClose) {
          const inner = sameLineClose[1]!.trim()

          start('code')
          nodes.push(
            <Box flexDirection="column" key={key} paddingLeft={2}>
              {inner ? <Text color={t.color.accent}>{renderMath(texToUnicode(inner))}</Text> : null}
            </Box>
          )
          i++

          continue
        }




        let closeIdx = -1

        for (let j = i + 1; j < lines.length; j++) {
          if (closeRe.test(lines[j]!)) {
            closeIdx = j

            break
          }
        }

        if (closeIdx < 0) {
          start('paragraph')
          nodes.push(<MdInline key={key} t={t} text={line} />)
          i++

          continue
        }

        if (headRest.trim()) {
          block.push(headRest)
        }

        for (let j = i + 1; j < closeIdx; j++) {
          block.push(lines[j]!)
        }

        const tail = lines[closeIdx]!.match(closeRe)![1]!.trimEnd()

        if (tail.trim()) {
          block.push(tail)
        }

        start('code')
        nodes.push(
          <Box flexDirection="column" key={key} paddingLeft={2}>
            {block.map((l, j) => (
              <Text color={t.color.accent} key={j}>
                {renderMath(texToUnicode(l))}
              </Text>
            ))}
          </Box>
        )
        i = closeIdx + 1

        continue
      }

      const heading = line.match(HEADING_RE)?.[2]

      if (heading) {
        start('heading')
        nodes.push(
          <Text bold color={t.color.accent} key={key} wrap="wrap-trim">
            <MdInline t={t} text={heading} />
          </Text>
        )
        i++

        continue
      }

      if (i + 1 < lines.length && SETEXT_RE.test(lines[i + 1]!)) {
        start('heading')
        nodes.push(
          <Text bold color={t.color.accent} key={key} wrap="wrap-trim">
            <MdInline t={t} text={line.trim()} />
          </Text>
        )
        i += 2

        continue
      }

      if (HR_RE.test(line)) {
        start('rule')
        nodes.push(
          <Text color={t.color.muted} key={key}>
            {'─'.repeat(36)}
          </Text>
        )
        i++

        continue
      }

      const footnote = line.match(FOOTNOTE_RE)

      if (footnote) {
        start('list')
        nodes.push(
          <Text color={t.color.muted} key={key} wrap="wrap-trim">
            [{footnote[1]}] <MdInline t={t} text={footnote[2] ?? ''} />
          </Text>
        )
        i++

        while (i < lines.length && /^\s{2,}\S/.test(lines[i]!)) {
          nodes.push(
            <Box key={`${key}-cont-${i}`} paddingLeft={2}>
              <Text color={t.color.muted} wrap="wrap-trim">
                <MdInline t={t} text={lines[i]!.trim()} />
              </Text>
            </Box>
          )
          i++
        }

        continue
      }

      if (i + 1 < lines.length && DEF_RE.test(lines[i + 1]!)) {
        start('list')
        nodes.push(
          <Text bold key={key} wrap="wrap-trim">
            {line.trim()}
          </Text>
        )
        i++

        while (i < lines.length) {
          const def = lines[i]!.match(DEF_RE)?.[1]

          if (!def) {
            break
          }

          nodes.push(
            <Text key={`${key}-def-${i}`} wrap="wrap-trim">
              <Text color={t.color.muted}> · </Text>
              <MdInline t={t} text={def} />
            </Text>
          )
          i++
        }

        continue
      }

      const bullet = line.match(BULLET_RE)

      if (bullet) {
        start('list')

        const task = bullet[2]!.match(TASK_RE)
        const marker = task ? (task[1]!.toLowerCase() === 'x' ? '☑' : '☐') : '•'

        nodes.push(
          <Box key={key} paddingLeft={indentDepth(bullet[1]!) * 2}>
            <Text wrap="wrap-trim">
              <Text color={t.color.muted}>{marker} </Text>
              <MdInline t={t} text={task ? task[2]! : bullet[2]!} />
            </Text>
          </Box>
        )
        i++

        continue
      }

      const numbered = line.match(NUMBERED_RE)

      if (numbered) {
        start('list')
        nodes.push(
          <Box key={key} paddingLeft={indentDepth(numbered[1]!) * 2}>
            <Text wrap="wrap-trim">
              <Text color={t.color.muted}>{numbered[2]}. </Text>
              <MdInline t={t} text={numbered[3]!} />
            </Text>
          </Box>
        )
        i++

        continue
      }

      if (QUOTE_RE.test(line)) {
        start('quote')

        const quoteLines: Array<{ depth: number; text: string }> = []

        while (i < lines.length && QUOTE_RE.test(lines[i]!)) {
          const prefix = lines[i]!.match(QUOTE_RE)?.[0] ?? ''

          quoteLines.push({ depth: (prefix.match(/>/g) ?? []).length, text: lines[i]!.slice(prefix.length) })
          i++
        }

        nodes.push(
          <Box flexDirection="column" key={key}>
            {quoteLines.map((ql, qi) => (
              <Box key={qi} paddingLeft={Math.max(0, ql.depth - 1) * 2}>
                <Text color={t.color.muted} wrap="wrap-trim">
                  │ <MdInline t={t} text={ql.text} />
                </Text>
              </Box>
            ))}
          </Box>
        )

        continue
      }

      if (line.includes('|') && i + 1 < lines.length && isTableDivider(lines[i + 1]!)) {
        start('table')

        const rows: string[][] = [splitRow(line)]

        for (i += 2; i < lines.length && lines[i]!.includes('|') && lines[i]!.trim(); i++) {
          rows.push(splitRow(lines[i]!))
        }

        nodes.push(renderTable(key, rows, t, avail))

        continue
      }

      if (/^<\/?details\b/i.test(line)) {
        i++

        continue
      }

      const summary = line.match(/^<summary>(.*?)<\/summary>$/i)?.[1]

      if (summary) {
        start('paragraph')
        nodes.push(
          <Text color={t.color.muted} key={key} wrap="wrap-trim">
            ▶ {summary}
          </Text>
        )
        i++

        continue
      }

      if (/^<\/?[^>]+>$/.test(line.trim())) {
        start('paragraph')
        nodes.push(
          <Text color={t.color.muted} key={key} wrap="wrap-trim">
            {line.trim()}
          </Text>
        )
        i++

        continue
      }

      if (line.includes('|') && line.trim().startsWith('|')) {
        start('table')

        const rows: string[][] = []

        while (i < lines.length && lines[i]!.trim().startsWith('|')) {
          const row = lines[i]!.trim()

          if (!/^[|\s:-]+$/.test(row)) {
            rows.push(splitRow(row))
          }

          i++
        }

        if (rows.length) {
          nodes.push(renderTable(key, rows, t, avail))
        }

        continue
      }

      start('paragraph')
      nodes.push(<MdInline key={key} t={t} text={line} />)
      i++
    }

    cacheSet(bucket, cacheKey, nodes)

    return nodes
  }, [avail, compact, t, text])

  return <Box flexDirection="column">{nodes}</Box>
}

export const Md = memo(MdImpl)

type Kind = 'blank' | 'code' | 'heading' | 'list' | 'paragraph' | 'quote' | 'rule' | 'table' | null

interface MdProps {


  avail?: number
  compact?: boolean
  t: Theme
  text: string
}
