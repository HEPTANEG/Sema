




import { describe, expect, it } from 'vitest'

import { findSlashCommand } from '../app/slash/registry.js'





const MUTATING_COMMANDS = [
  'background',
  'branch',
  'browser',
  'busy',
  'clear',
  'compress',
  'fast',
  'model',
  'new',
  'personality',
  'queue',
  'reasoning',
  'retry',
  'rollback',
  'steer',
  'stop',
  'title',
  'tools',
  'undo',
  'verbose',
  'voice',
  'yolo'
] as const

describe('slash routing', () => {
  it('resolves every mutating command locally instead of the CLI slash worker', () => {
    expect(MUTATING_COMMANDS.filter(name => !findSlashCommand(name))).toEqual([])
  })

  it('leaves an unknown command unresolved so it reaches the CLI', () => {
    expect(findSlashCommand('channels-status')).toBeUndefined()
  })

  it('resolves aliases and is case-insensitive', () => {
    expect(findSlashCommand('MODEL')).toBe(findSlashCommand('model'))
    expect(findSlashCommand('bg')).toBe(findSlashCommand('background'))
  })
})
