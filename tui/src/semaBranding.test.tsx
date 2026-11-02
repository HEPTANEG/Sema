import {renderSync} from '@hermes/ink'
import React from 'react'
import {PassThrough} from 'stream'
import {describe, expect, it} from 'vitest'

import {ravenLogo, ravenLogoWord, RAVEN_WORD_WIDTH} from './raven/banner.js'
import {SessionPanel, StartupLoader} from './raven/components/branding.js'
import {stripAnsi} from './raven/lib/text.js'
import {DEFAULT_THEME} from './raven/theme.js'

const renderFrame = (component: React.ReactNode) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, {columns: 100, isTTY: true, rows: 24})
  Object.assign(stdin, {isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {}})
  Object.assign(stderr, {isTTY: true})
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(<>{component}</>, {
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })
  instance.unmount()
  instance.cleanup()
  return stripAnsi(output)
}

describe('Sema Raven branding', () => {
  it('uses Sema brand defaults and cyan theme colors', () => {
    expect(DEFAULT_THEME.brand.name).toBe('Sema')
    expect(DEFAULT_THEME.brand.icon).toBe('S')
    expect(DEFAULT_THEME.color.primary).toBe('#20d8ff')
    expect(DEFAULT_THEME.color.prompt).toBe('#20d8ff')
  })

  it('renders the compact SEMA wordmark asset', () => {
    const lines = ravenLogo(DEFAULT_THEME.yellow)
    const textRows = lines.map(([, text]) => text)
    expect(lines.length).toBe(8)
    expect(textRows.join('')).toContain('█')
    expect(textRows[2].slice(0, 10)).toContain('░███  ░')
    expect(textRows[2].slice(0, 10)).not.toContain('░███  █')
    expect(textRows[4]).toContain('░░░░░███')
    expect(textRows[5]).toContain('      ░███')
    expect(RAVEN_WORD_WIDTH).toBeLessThan(70)
    expect(ravenLogoWord(DEFAULT_THEME.yellow).map(([, text]) => text)).toEqual(textRows)
  })

  it('shows only the approved startup panel fields', () => {
    const frame = renderFrame(
      <SessionPanel
        info={{cwd: 'C:\\Users\\ExampleUser', model: 'deepseek:deepseek-chat', provider: 'deepseek', tools: {}}}
        maxCols={100}
        sid="event-stream"
        t={DEFAULT_THEME}
      />
    )
    expect(frame).toContain('Model:')
    expect(frame).toContain('deepseek:deepseek-chat')
    expect(frame).toContain('CWD:')
    expect(frame).toContain('C:\\Users\\ExampleUser')
    expect(frame).toContain('Commands:')
    expect(frame).toContain('/help')
    expect(frame).not.toContain('Agent:')
    expect(frame).not.toContain('Provider:')
    expect(frame).not.toContain('Session:')
    expect(frame).not.toContain('Conversation:')
    expect(frame).not.toContain('Permissions:')
    expect(frame).not.toContain('event-stream')
  })

  it('uses Sema startup loader copy', () => {
    const frame = renderFrame(<StartupLoader t={DEFAULT_THEME} />)
    expect(frame).toContain('starting')
    expect(frame).toContain('Sema')
  })
})
