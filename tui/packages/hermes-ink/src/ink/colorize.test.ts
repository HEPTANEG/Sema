





import chalk from 'chalk'
import { afterEach, describe, expect, it } from 'vitest'

import { activeColorTier, colorize, shouldClampForAppleTerminal } from './colorize.js'

const originalLevel = chalk.level

afterEach(() => {
  chalk.level = originalLevel
})

describe('shouldClampForAppleTerminal', () => {
  it('clamps Apple Terminal when level claims truecolor', () => {


    expect(shouldClampForAppleTerminal({ TERM_PROGRAM: 'Apple_Terminal' }, 3)).toBe(true)
  })

  it('leaves Apple Terminal alone at 256 or below', () => {
    expect(shouldClampForAppleTerminal({ TERM_PROGRAM: 'Apple_Terminal' }, 2)).toBe(false)
  })

  it('does not touch other terminals', () => {
    expect(shouldClampForAppleTerminal({ TERM_PROGRAM: 'iTerm.app' }, 3)).toBe(false)
    expect(shouldClampForAppleTerminal({}, 3)).toBe(false)
  })
})

describe('activeColorTier', () => {
  it('reflects chalk.level', () => {
    chalk.level = 2
    expect(activeColorTier()).toBe(2)
    chalk.level = 0
    expect(activeColorTier()).toBe(0)
  })
})

describe('colorize emits the value verbatim (no homebrew downsample)', () => {
  it('emits a 24-bit SGR for hex at truecolor', () => {
    chalk.level = 3


    expect(colorize('x', '#4F7A45', 'foreground')).toContain('[38;2;79;122;69m')
  })

  it('emits a 256-color SGR for ansi256 at level 2', () => {
    chalk.level = 2
    expect(colorize('x', 'ansi256(65)', 'foreground')).toContain('[38;5;65m')
  })

  it('emits a named 16-color SGR for ansi:green', () => {
    chalk.level = 2
    expect(colorize('x', 'ansi:green', 'foreground')).toContain('[32m')
  })

  it('strips color entirely at level 0', () => {
    chalk.level = 0
    expect(colorize('x', '#4F7A45', 'foreground')).toBe('x')
    expect(colorize('x', 'ansi256(65)', 'foreground')).toBe('x')
  })

  it('returns the string unchanged when no color is given', () => {
    chalk.level = 3
    expect(colorize('x', undefined, 'foreground')).toBe('x')
  })
})
