




















export type ColorTier = 0 | 1 | 2 | 3

const TRUE_RE = /^(?:1|true|yes|on)$/i





export function parseColorOverride(env: NodeJS.ProcessEnv = process.env): ColorTier | null {
  const raw = (env.RAVEN_TUI_COLOR ?? '').trim().toLowerCase()

  switch (raw) {
    case 'none':
    case 'off':
    case '0':
      return 0
    case '16':
    case 'ansi':
    case '1':
      return 1
    case '256':
    case 'ansi256':
    case '2':
      return 2
    case 'truecolor':
    case '24bit':
    case 'rgb':
    case '3':
      return 3
    case '':
    case 'auto':
      break
    default:
      break
  }


  if (TRUE_RE.test((env.RAVEN_TUI_TRUECOLOR ?? '').trim())) {
    return 3
  }

  return null
}






export function applyColorOverride(env: NodeJS.ProcessEnv = process.env): ColorTier | null {


  if ('NO_COLOR' in env) {
    env.HERMES_TUI_LEVEL = '0'
    env.FORCE_COLOR = '0'
    return 0
  }

  const tier = parseColorOverride(env)

  if (tier === null) {
    return null
  }


  env.HERMES_TUI_LEVEL = String(tier)

  if (tier === 0) {
    env.FORCE_COLOR = '0'
  }

  return tier
}

applyColorOverride()

export {}
