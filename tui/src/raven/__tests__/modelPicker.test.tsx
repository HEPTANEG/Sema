



import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it, vi } from 'vitest'

import type { ModelOptionProvider } from '../gatewayTypes.js'

import { ModelPicker } from '../components/modelPicker.js'
import { DEFAULT_THEME } from '../theme.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const waitForFrame = async (h: Pick<Harness, 'frame'>, text: string) => {
  for (let i = 0; i < 20; i++) {
    if (h.frame().includes(text)) {
      return
    }
    await delay(30)
  }
  expect(h.frame()).toContain(text)
}

const ESC_RE = new RegExp(String.fromCharCode(27), 'g')




const normalize = (raw: string) =>
  raw
    .replace(new RegExp(`${String.fromCharCode(27)}\\[[0-9;?<>=]*[a-zA-Z]`, 'g'), ' ')
    .replace(new RegExp(`${String.fromCharCode(27)}\\][^\\u0007]*\\u0007?`, 'g'), ' ')
    .replace(ESC_RE, ' ')
    .replace(/\s+/g, ' ')

const ENTER = '\r'
const DOWN = '[B'

const ESCAPE = String.fromCharCode(27)

const anthropic: ModelOptionProvider = {
  auth_type: 'key',
  authenticated: true,
  is_current: true,
  key_env: 'ANTHROPIC_API_KEY',
  models: ['claude-sonnet-4-6'],
  name: 'Anthropic',
  needs_api_base: false,
  slug: 'anthropic',
  total_models: 1
}

const ollama: ModelOptionProvider = {
  auth_type: 'local',
  authenticated: false,
  is_current: false,
  key_env: null,
  models: [],
  name: 'Ollama (local)',
  needs_api_base: true,
  slug: 'ollama_chat',
  total_models: 0,
  warning: 'enter the server address to activate'
}

const custom: ModelOptionProvider = {
  auth_type: 'endpoint',
  authenticated: false,
  is_current: false,
  key_env: null,
  models: [],
  name: 'Custom',
  needs_api_base: true,
  slug: 'custom',
  total_models: 0,
  warning: 'set key + base to activate'
}

const deepseek: ModelOptionProvider = {
  auth_type: 'key',
  authenticated: true,
  is_current: true,
  key_env: 'DEEPSEEK_API_KEY',
  models: ['deepseek-chat'],
  name: 'DeepSeek',
  needs_api_base: false,
  slug: 'deepseek',
  total_models: 1
}

const oauthProvider: ModelOptionProvider = {
  auth_type: 'oauth',
  authenticated: false,
  is_current: false,
  key_env: null,
  models: [],
  name: 'MiniMax Global',
  needs_api_base: false,
  slug: 'minimax_global',
  total_models: 0,
  warning: 'run `raven provider login minimax-global` to authenticate'
}

interface MountExtras {

  launch?: { code: null | number; error?: string }

  nextProviders?: ModelOptionProvider[]

  scope?: 'default' | 'session'
}

interface Harness {
  frame: () => string
  gw: { request: ReturnType<typeof vi.fn> }
  onCancel: ReturnType<typeof vi.fn>
  launchedInsideSuspend: () => boolean
  launcher: ReturnType<typeof vi.fn>
  onSelect: ReturnType<typeof vi.fn>
  optionsCalls: () => number
  suspend: ReturnType<typeof vi.fn>
  type: (s: string) => Promise<void>
  unmount: () => void
}

const mount = (
  providers: ModelOptionProvider[],
  requestImpl?: (m: string, p: any) => unknown,
  extras?: MountExtras
): Harness => {
  const onSelect = vi.fn()
  const onCancel = vi.fn()
  let optionsCalls = 0
  const request = vi.fn((method: string, params: Record<string, unknown>) => {
    if (method === 'model.options') {
      optionsCalls += 1
      const list = optionsCalls > 1 && extras?.nextProviders ? extras.nextProviders : providers

      return Promise.resolve({ model: 'claude-sonnet-4-6', provider: 'anthropic', providers: list })
    }

    return Promise.resolve(requestImpl ? requestImpl(method, params) : {})
  })
  const gw = { request } as unknown as { request: typeof request }




  let insideSuspend = false
  let launchedInsideSuspend = false
  const launcher = vi.fn(async (_args: string[]) => {
    launchedInsideSuspend = insideSuspend

    return extras?.launch ?? { code: 0 }
  })
  const suspend = vi.fn(async (run: () => Promise<void>) => {
    insideSuspend = true
    try {
      await run()
    } finally {
      insideSuspend = false
    }
  })

  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: true })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    <ModelPicker
      gw={gw as never}
      launcher={launcher as never}
      onCancel={onCancel}
      onSelect={onSelect}
      scope={extras?.scope ?? 'session'}
      sessionId="tui:session-1"
      suspend={suspend as never}
      t={DEFAULT_THEME}
    />,
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  return {
    frame: () => normalize(output),
    gw: gw as never,
    launchedInsideSuspend: () => launchedInsideSuspend,
    launcher,
    onCancel,
    onSelect,
    optionsCalls: () => optionsCalls,
    suspend,
    type: async (s: string) => {
      stdin.write(s)
      await delay(30)
    },
    unmount: () => {
      instance.unmount()
      instance.cleanup()
    }
  }
}

describe('ModelPicker', () => {
  it('opens with the cursor on the provider in use', async () => {





    const cfg = (slug: string, name: string, authed: boolean, cur = false): ModelOptionProvider =>
      ({
        auth_type: 'key',
        authenticated: authed,
        is_current: cur,
        key_env: null,
        models: authed ? [`${slug}/m1`] : [],
        name,
        needs_api_base: false,
        slug,
        total_models: authed ? 1 : 0
      }) as ModelOptionProvider


    const h = mount([
      cfg('openrouter', 'OpenRouter', true),
      cfg('openai', 'OpenAI', false),
      cfg('anthropic', 'Anthropic', false),
      cfg('deepseek', 'DeepSeek', true, true),
      cfg('gemini', 'Gemini', false)
    ])
    await delay(80)

    await h.type(ENTER)
    const frame = h.frame()
    expect(frame).toContain('deepseek/m1')
    expect(frame).not.toContain('openrouter/m1')
    h.unmount()
  })

  it('says which scope the selection will apply', async () => {









    const session = mount([anthropic])
    await delay(80)
    await session.type(ENTER)
    const sessionFrame = session.frame()
    session.unmount()

    const asDefault = mount([anthropic], undefined, { scope: 'default' })
    await delay(80)
    await asDefault.type(ENTER)
    const defaultFrame = asDefault.frame()
    asDefault.unmount()

    expect(sessionFrame).toContain('--default sets')
    expect(defaultFrame).not.toContain('--default sets')
    expect(defaultFrame).not.toBe(sessionFrame)
  })

  it('lists what is set up, and puts the rest behind one row', async () => {



    const h = mount([anthropic, custom, ollama, oauthProvider])
    await delay(60)

    const first = h.frame()
    expect(first).toContain('Anthropic')
    expect(first).toContain('add a provider')
    expect(first).toContain('3 not set up')
    expect(first).not.toContain('Custom')
    expect(first).not.toContain('Ollama')


    await h.type(DOWN)
    await h.type(ENTER)

    const second = h.frame()
    expect(second).toContain('Add a provider')
    expect(second).toContain('Ollama')


    await h.type('\u001b')
    await delay(30)
    expect(h.frame()).toContain('Select pr')
    h.unmount()
  })

  it('offers a local deployment its address, and says it needs no key', async () => {



    const h = mount([anthropic, ollama])
    await delay(60)



    expect(h.frame()).not.toContain('Ollama')
    await h.type(DOWN)
    await h.type(ENTER)

    expect(h.frame()).toContain('(no address)')
    await h.type(ENTER)




    const frame = h.frame()
    expect(frame).toContain('Ollama (local)')
    expect(frame).toContain('Server address')

    expect(frame).not.toContain('API key')



    await h.type('http://gpu-box:11434')
    await h.type(ENTER)
    await delay(30)

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.save_key',
      expect.objectContaining({ api_base: 'http://gpu-box:11434', slug: 'ollama_chat' })
    )
    const call = (h.gw.request as unknown as { mock: { calls: unknown[][] } }).mock.calls.find(
      c => c[0] === 'model.save_key'
    )
    expect((call?.[1] as { api_key?: string }).api_key).toBe('')
    h.unmount()
  })

  it('shows the api_base field and requires it for a needs_api_base provider', async () => {
    const h = mount([anthropic, custom])
    await delay(60)


    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)

    const keyFrame = h.frame()
    expect(keyFrame).toContain('Custom')
    expect(keyFrame).toContain('API key')
    expect(keyFrame).toContain('API base (required)')


    await h.type('sk-test')
    await h.type(ENTER)
    await h.type(ENTER)

    expect(h.frame()).toContain('API base URL is required')
    expect(h.gw.request).not.toHaveBeenCalledWith('model.save_key', expect.anything())


    await h.type('https://api.example.com')
    await h.type(ENTER)

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.save_key',
      expect.objectContaining({ api_base: 'https://api.example.com', api_key: 'sk-test', slug: 'custom' })
    )

    h.unmount()
  })

  it('sends OAuth providers to the sign-in screen, not the key prompt', async () => {
    const h = mount([anthropic, oauthProvider])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    expect(h.frame()).toContain('raven provider login')

    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')


    expect(h.gw.request).not.toHaveBeenCalledWith('model.save_key', expect.anything())
    expect(h.launcher).not.toHaveBeenCalled()

    h.unmount()
  })

  it('lets a key with a q in it be typed', async () => {



    const h = mount([anthropic, custom])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Tab switches field')



    for (const ch of 'sk-q1') {
      await h.type(ch)
    }



    expect(h.onCancel).not.toHaveBeenCalled()

    h.unmount()
  })

  it('runs `provider login` inside the Ink suspend on the sign-in screen', async () => {
    const h = mount([anthropic, oauthProvider])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ENTER)
    await delay(60)

    expect(h.launcher).toHaveBeenCalledWith(['provider', 'login', 'minimax-global'])
    expect(h.suspend).toHaveBeenCalledTimes(1)
    expect(h.launchedInsideSuspend()).toBe(true)

    h.unmount()
  })

  it('lands on the signed-in provider after a sign-in that took', async () => {




    const signedIn = { ...oauthProvider, authenticated: true, models: ['MiniMax-M2'], total_models: 1 }
    const h = mount([custom, oauthProvider, anthropic], undefined, {
      nextProviders: [custom, signedIn, anthropic]
    })
    await delay(60)



    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(DOWN)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')
    expect(h.optionsCalls()).toBe(1)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')

    expect(h.optionsCalls()).toBe(2)
    await h.type(ENTER)
    await delay(30)



    expect(h.onSelect).toHaveBeenCalledWith('MiniMax-M2', 'minimax_global')

    h.unmount()
  })

  it('says so when disconnecting the provider the session is using', async () => {



    const h = mount([{ ...deepseek, is_current: true }, oauthProvider])
    await delay(60)

    await h.type('d')
    await waitForFrame(h, 'serves your current model')

    h.unmount()
  })

  it('keeps the sign-in screen when the login command fails', async () => {
    const h = mount([anthropic, oauthProvider], undefined, { launch: { code: 1 } })
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ENTER)
    await waitForFrame(h, 'exited with code 1')


    expect(h.optionsCalls()).toBe(1)
    expect(h.frame()).not.toContain('step 2/2')

    h.unmount()
  })

  it('treats a signal-killed login as the cancel it promises, not a fault', async () => {



    const h = mount([anthropic, oauthProvider], undefined, { launch: { code: null } })
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ENTER)
    await delay(60)

    expect(h.frame()).not.toContain('exited with code')
    expect(h.frame()).toContain('Sign in to MiniMax Global?')

    h.unmount()
  })

  it('returns from the sign-in screen to the list it was opened from', async () => {
    const h = mount([anthropic, oauthProvider])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ESCAPE)
    await delay(30)





    await h.type(ENTER)
    await h.type(ENTER)
    await delay(60)

    expect(h.launcher).toHaveBeenCalledWith(['provider', 'login', 'minimax-global'])
    expect(h.frame()).not.toContain('step 2/2')

    h.unmount()
  })

  it('re-anchors the cursor when a sign-in empties the row it came from', async () => {




    const other = { ...oauthProvider, name: 'Second Unconfigured', slug: 'second_unconfigured' }
    const h = mount([anthropic, oauthProvider, other], undefined, {
      nextProviders: [
        anthropic,
        { ...oauthProvider, authenticated: true, models: ['MiniMax-M2'], total_models: 1 },
        other
      ]
    })
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')






    await h.type(ESCAPE)
    await h.type(ENTER)
    await h.type(ENTER)
    await delay(60)

    expect(h.launcher).toHaveBeenCalledTimes(1)
    expect(h.launcher).not.toHaveBeenCalledWith(['provider', 'login', 'second-unconfigured'])

    h.unmount()
  })

  it('reports a sign-in that leaves no credentials behind', async () => {
    const h = mount([anthropic, oauthProvider])
    await delay(60)

    await h.type(DOWN)
    await h.type(ENTER)
    await h.type(ENTER)
    await waitForFrame(h, 'Sign in to MiniMax Global?')

    await h.type(ENTER)
    await waitForFrame(h, 'still finds no credentials')

    expect(h.optionsCalls()).toBe(2)

    h.unmount()
  })

  it('adds a model name via model.add_model and refreshes the list', async () => {
    const h = mount([anthropic], (method, params) => {
      if (method === 'model.add_model') {
        return { provider: { ...anthropic, models: [...anthropic.models!, params.model], total_models: 2 } }
      }

      return {}
    })
    await delay(60)


    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')


    await h.type('a')
    expect(h.frame()).toContain('Type the full model id')

    await h.type('claude-opus-4')
    await h.type(ENTER)

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.add_model',
      expect.objectContaining({ model: 'claude-opus-4', slug: 'anthropic' })
    )

    h.unmount()
  })

  it('removes the selected model name via model.remove_model', async () => {
    const twoModels = { ...anthropic, models: ['claude-sonnet-4-6', 'claude-opus-4'], total_models: 2 }
    const h = mount([twoModels], method => {
      if (method === 'model.remove_model') {
        return { provider: { ...twoModels, models: ['claude-opus-4'], total_models: 1 } }
      }

      return {}
    })
    await delay(60)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')
    expect(h.frame()).toContain('claude-sonnet-4-6')


    await h.type('d')

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.remove_model',
      expect.objectContaining({ model: 'claude-sonnet-4-6', slug: 'anthropic' })
    )

    h.unmount()
  })

  it('lists a provider endpoints, masked, on `e`', async () => {
    const h = mount([anthropic], method => {
      if (method === 'model.endpoints') {
        return {
          endpoints: [
            { api_base: 'https://eu.example.test/v1', api_key: '****set****', label: 'eu' },
            { api_base: null, api_key: '(empty)', label: 'spare' }
          ]
        }
      }

      return {}
    })
    await delay(60)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')

    await h.type('e')
    await waitForFrame(h, 'eu · ****set**** · https://eu.example.test/v1')

    expect(h.gw.request).toHaveBeenCalledWith('model.endpoints', expect.objectContaining({ slug: 'anthropic' }))


    expect(h.frame()).toContain('spare · (empty)')

    h.unmount()
  })

  it('adds an endpoint via model.add_endpoint with all three fields', async () => {
    const h = mount([anthropic], (method, params) => {
      if (method === 'model.endpoints') {
        return { endpoints: [] }
      }

      if (method === 'model.add_endpoint') {
        return { endpoints: [{ api_base: params.api_base, api_key: '****set****', label: params.label }] }
      }

      return {}
    })
    await delay(60)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')
    await h.type('e')
    await waitForFrame(h, 'no endpoints')


    await h.type('a')
    await h.type('eu')
    await h.type(ENTER)
    await h.type('sk-eu')
    await h.type(ENTER)
    await h.type('https://eu.example.test/v1')
    await h.type(ENTER)

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.add_endpoint',
      expect.objectContaining({
        api_base: 'https://eu.example.test/v1',
        api_key: 'sk-eu',
        label: 'eu',
        slug: 'anthropic'
      })
    )

    await waitForFrame(h, 'eu · ****set****')

    h.unmount()
  })

  it('refuses to submit an endpoint with no key for a key-based provider', async () => {



    const h = mount([anthropic], method => (method === 'model.endpoints' ? { endpoints: [] } : {}))
    await delay(60)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')
    await h.type('e')
    await waitForFrame(h, 'no endpoints')


    await h.type('a')
    await h.type('eu')
    await h.type(ENTER)
    await h.type(ENTER)
    await h.type(ENTER)

    await waitForFrame(h, 'error: API key is required')
    expect(h.gw.request).not.toHaveBeenCalledWith('model.add_endpoint', expect.anything())

    h.unmount()
  })

  it('allows submitting an endpoint with no key for a local deployment', async () => {
    const ollamaConfigured: ModelOptionProvider = { ...ollama, authenticated: true }
    const h = mount([ollamaConfigured], (method, params) => {
      if (method === 'model.endpoints') {
        return { endpoints: [] }
      }

      if (method === 'model.add_endpoint') {
        return { endpoints: [{ api_base: params.api_base, api_key: '(empty)', label: params.label }] }
      }

      return {}
    })
    await delay(60)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')
    await h.type('e')



    await waitForFrame(h, 'a adds one')


    await h.type('a')
    await h.type('local')
    await h.type(ENTER)
    await h.type(ENTER)
    await h.type('http://10.0.0.5:8000/v1')
    await h.type(ENTER)

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.add_endpoint',
      expect.objectContaining({ label: 'local', slug: 'ollama_chat' })
    )

    h.unmount()
  })

  it('removes the selected endpoint via model.remove_endpoint', async () => {
    const h = mount([anthropic], method => {
      if (method === 'model.endpoints') {
        return {
          endpoints: [
            { api_base: null, api_key: '****set****', label: 'eu' },
            { api_base: null, api_key: '****set****', label: 'us' }
          ]
        }
      }

      if (method === 'model.remove_endpoint') {
        return { endpoints: [{ api_base: null, api_key: '****set****', label: 'us' }] }
      }

      return {}
    })
    await delay(60)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')
    await h.type('e')
    await waitForFrame(h, 'eu · ****set****')

    await h.type(DOWN)
    await h.type('d')

    expect(h.gw.request).toHaveBeenCalledWith(
      'model.remove_endpoint',
      expect.objectContaining({ label: 'us', slug: 'anthropic' })
    )

    h.unmount()
  })

  it('returns from the endpoint list to the model list on Esc', async () => {
    const h = mount([anthropic], method => (method === 'model.endpoints' ? { endpoints: [] } : {}))
    await delay(60)

    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')
    await h.type('e')
    await waitForFrame(h, 'no endpoints')



    await h.type(ESCAPE)


    await delay(120)
    await h.type(ENTER)
    await delay(30)

    expect(h.onSelect).toHaveBeenCalledWith('claude-sonnet-4-6', 'anthropic')

    h.unmount()
  })

  it('emits a structured model + provider selection on Enter', async () => {
    const h = mount([anthropic])
    await delay(60)

    await h.type(ENTER)
    await h.type(ENTER)

    expect(h.onSelect).toHaveBeenCalledWith('claude-sonnet-4-6', 'anthropic')

    h.unmount()
  })
})

describe('model labels', () => {
  const described: ModelOptionProvider = {
    ...anthropic,
    model_labels: {
      'claude-sonnet-4-6': {
        context: 1000000,
        description: 'Claude workhorse for coding agents',
        label: 'Claude Sonnet 4.6'
      }
    },
    models: ['claude-sonnet-4-6', 'some-unlisted-finetune'],
    total_models: 2
  }

  it('shows a name and a description beside the id, and the id alone without one', async () => {
    const h = mount([described])
    await waitForFrame(h, 'Anthropic')
    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')

    const frame = normalize(h.frame())

    expect(frame).toContain('Claude Sonnet 4.6 · claude-sonnet-4-6')
    expect(frame).toContain('Claude workhorse for coding agents')

    expect(frame).toContain('some-unlisted-finetune')

    h.unmount()
  })

  it('renders ids unchanged for a provider the payload describes nothing for', async () => {
    const h = mount([anthropic])
    await waitForFrame(h, 'Anthropic')
    await h.type(ENTER)
    await waitForFrame(h, 'step 2/2')

    expect(normalize(h.frame())).toContain('claude-sonnet-4-6')
    h.unmount()
  })
})
