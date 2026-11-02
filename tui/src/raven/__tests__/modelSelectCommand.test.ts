




import { describe, expect, it } from 'vitest'

import { modelSelectCommand } from '../app/useMainApp.js'

describe('modelSelectCommand', () => {
  it('carries --default through the picker that was opened with it', () => {




    expect(modelSelectCommand('claude-opus-4-5', 'anthropic', 'default')).toBe(
      '/model claude-opus-4-5 --provider anthropic --default'
    )
  })

  it('leaves a plain selection scoped to this conversation', () => {


    expect(modelSelectCommand('claude-opus-4-5', 'anthropic', true)).toBe('/model claude-opus-4-5 --provider anthropic')
  })

  it('treats a closed picker as session scope', () => {



    expect(modelSelectCommand('m', 'p', false)).toBe('/model m --provider p')
  })

  it('spells the flag form the parser accepts', () => {




    const command = modelSelectCommand('anthropic/claude-haiku-4-5', 'openrouter', 'default')

    expect(command).toBe('/model anthropic/claude-haiku-4-5 --provider openrouter --default')
    expect(command.replace(/(^|\s)--default(?=\s|$)/g, '').trim()).toBe(
      '/model anthropic/claude-haiku-4-5 --provider openrouter'
    )
  })
})
