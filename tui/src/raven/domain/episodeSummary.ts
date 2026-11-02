



import type { Episode, EpisodeTool } from '../types.js'





interface VerbRule {
  verb: string
  unit: string
  style: 'count' | 'target'
}







const OVERRIDES: Record<string, VerbRule> = {
  read_file: { verb: 'read', unit: 'files', style: 'count' },
  grep: { verb: 'searched', unit: 'patterns', style: 'count' },
  find: { verb: 'found', unit: 'files', style: 'count' },
  list_dir: { verb: 'listed', unit: 'dirs', style: 'count' },
  web_search: { verb: 'searched', unit: 'queries', style: 'target' },
  web_fetch: { verb: 'fetched', unit: 'urls', style: 'target' },
  exec: { verb: 'ran', unit: 'commands', style: 'target' },
  edit_file: { verb: 'edited', unit: 'files', style: 'target' },
  write_file: { verb: 'wrote', unit: 'files', style: 'target' },
  deep_research: { verb: 'researched', unit: '', style: 'target' },
  cron: { verb: 'scheduled', unit: '', style: 'target' },
  ask_user: { verb: 'asked', unit: '', style: 'target' },
  spawn: { verb: 'delegated', unit: 'subagents', style: 'count' }
}




const humanize = (name: string) => name.split('_').filter(Boolean).join(' ')




const ruleFor = (name: string): VerbRule =>
  OVERRIDES[name] ?? { verb: humanize(name) || name, unit: 'calls', style: 'target' }


const QUOTED = new Set(['grep', 'find', 'web_search'])




const PATHY = new Set(['read_file', 'write_file', 'edit_file', 'list_dir'])

const titleName = (name: string) =>
  name
    .split('_')
    .filter(Boolean)
    .map(p => p[0]!.toUpperCase() + p.slice(1))
    .join(' ') || name

const clip = (s: string, n = 40) => {
  const one = s.replace(/\s+/g, ' ').trim()

  return one.length > n ? `${one.slice(0, n - 1)}…` : one
}



const target = (tool: EpisodeTool): string => {
  const raw = tool.summary.trim()

  if (!raw) {
    return ''
  }

  if (
    tool.name === 'read_file' ||
    tool.name === 'write_file' ||
    tool.name === 'edit_file' ||
    tool.name === 'list_dir'
  ) {
    return clip(raw.split(/[\\/]/).pop() || raw)
  }

  if (QUOTED.has(tool.name)) {
    return `"${clip(raw, 32)}"`
  }

  return clip(raw)
}

const phraseFor = (tools: EpisodeTool[]): string => {
  const rule = ruleFor(tools[0]!.name)
  const verb = rule.verb

  if (tools.length === 1) {
    const only = tools[0]!
    const t = target(only)
    const base = t ? `${verb} ${t}` : verb || titleName(only.name)
    const stat = only.added != null || only.removed != null ? ` (+${only.added ?? 0} -${only.removed ?? 0})` : ''

    return `${base}${stat}`
  }

  const unit = rule.unit || 'calls'

  return `${verb} ${tools.length} ${unit}`
}



const clipPath = (s: string, n = 60): string => {
  const one = s.replace(/\s+/g, ' ').trim()

  return one.length > n ? `…${one.slice(one.length - (n - 1))}` : one
}






export const toolParts = (tool: EpisodeTool): { verb: string; detail: string } => {
  const verb = ruleFor(tool.name).verb
  const raw = tool.summary.trim()
  const stat = tool.added != null || tool.removed != null ? `+${tool.added ?? 0} -${tool.removed ?? 0}` : ''


  const arg = QUOTED.has(tool.name)
    ? raw
      ? `"${clip(raw, 120)}"`
      : ''
    : PATHY.has(tool.name)
      ? clipPath(raw, 120)
      : clip(raw, 120)
  const detail = [arg, stat].filter(Boolean).join(' ')

  return { verb: verb || titleName(tool.name), detail }
}




export const groupTools = (tools: EpisodeTool[]): EpisodeTool[][] => {
  const groups: EpisodeTool[][] = []

  for (const tool of tools) {
    const last = groups.at(-1)

    if (last && last[0]!.name === tool.name) {
      last.push(tool)
    } else {
      groups.push([tool])
    }
  }

  return groups
}



export const toolsSummary = (tools: EpisodeTool[]): string => groupTools(tools).map(phraseFor).join(', ')


export const episodeFailed = (ep: Episode): boolean => ep.tools.some(t => !t.ok)
