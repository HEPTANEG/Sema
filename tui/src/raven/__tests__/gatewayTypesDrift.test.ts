
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'















function propertyNames(source: string, interfaceName: string): Set<string> {
  const start = source.indexOf(`export interface ${interfaceName} {`)
  if (start < 0) {
    throw new Error(`${interfaceName} not found`)
  }
  const body = source.slice(start, source.indexOf('\n}', start))
  const names = new Set<string>()
  for (const line of body.split('\n').slice(1)) {
    const match = /^\s{2}([A-Za-z_][A-Za-z0-9_]*)\??\s*:/.exec(line)
    if (match) {
      names.add(match[1])
    }
  }
  return names
}

function read(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')
}

describe('ModelOptionProvider', () => {
  it('declares every property the generated contract sends', () => {
    const generated = propertyNames(read('../rpc/generated.ts'), 'ModelOptionProvider')
    const handWritten = propertyNames(read('../gatewayTypes.ts'), 'ModelOptionProvider')

    expect(generated.size).toBeGreaterThan(0)
    const missing = [...generated].filter(name => !handWritten.has(name)).sort()
    expect(missing, `add these to gatewayTypes.ts ModelOptionProvider: ${missing.join(', ')}`).toEqual([])
  })
})
