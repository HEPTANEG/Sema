








const PREVIEW_KEYS = [
  'question',
  'query',
  'q',
  'command',
  'cmd',
  'pattern',
  'path',
  'file',
  'url',
  'prompt',
  'text',
  'goal',
  'task',
  'name'
]





const firstStringArg = (value: unknown, depth = 0): string | undefined => {
  if (typeof value === 'string') {
    return value.trim() || undefined
  }

  if (depth >= 4 || value == null || typeof value !== 'object') {
    return undefined
  }

  if (Array.isArray(value)) {
    for (const item of value) {
      const found = firstStringArg(item, depth + 1)

      if (found) {
        return found
      }
    }

    return undefined
  }

  const obj = value as Record<string, unknown>

  for (const key of PREVIEW_KEYS) {
    const found = firstStringArg(obj[key], depth + 1)

    if (found) {
      return found
    }
  }

  for (const val of Object.values(obj)) {
    const found = firstStringArg(val, depth + 1)

    if (found) {
      return found
    }
  }

  return undefined
}




export const argPreview = (args: Record<string, unknown>): string => firstStringArg(args) ?? ''
