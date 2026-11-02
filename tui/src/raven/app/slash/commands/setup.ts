import type { SlashCommand } from '../types.js'

import { launchRavenCommand } from '../../../lib/externalCli.js'
import { suspendForHandoff } from '../../../lib/handoff.js'
import { runExternalSetup } from '../../setupHandoff.js'

export const setupCommands: SlashCommand[] = [
  {


    help: 'run full setup wizard (launches `raven onboard`)',
    name: 'setup',


    supported: false,
    run: (arg, ctx) =>
      void runExternalSetup({
        args: ['onboard', ...arg.split(/\s+/).filter(Boolean)],
        ctx,
        done: 'setup complete — starting session…',
        launcher: launchRavenCommand,
        suspend: suspendForHandoff
      })
  }
]
