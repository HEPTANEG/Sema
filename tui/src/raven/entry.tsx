












import './lib/colorTier.js'

import type { FrameEvent } from '@hermes/ink'

import { activeColorTier } from '@hermes/ink'

import type { GatewayClient } from './gatewayClientStub.js'

import { GatewayClientCompat } from './gatewayClientCompat.js'
import { setupGracefulExit } from './lib/gracefulExit.js'
import { formatBytes, type HeapDumpResult, performHeapDump } from './lib/memory.js'
import { type MemorySnapshot, startMemoryMonitor } from './lib/memoryMonitor.js'
import { renderColorPreview, renderColorSwatches } from './lib/printColors.js'
import { resetTerminalModes } from './lib/terminalModes.js'
import { DEFAULT_THEME } from './theme.js'




if (process.env.RAVEN_TUI_PRINT_COLORS === '1') {
  process.stdout.write(renderColorSwatches(DEFAULT_THEME, activeColorTier()))
  process.exit(0)
}




if (process.env.RAVEN_TUI_COLOR_PREVIEW === '1') {
  process.stdout.write(renderColorPreview(DEFAULT_THEME, activeColorTier()))
  process.exit(0)
}

if (!process.stdin.isTTY) {
  console.log('raven-tui: no TTY')
  process.exit(0)
}



resetTerminalModes()










if (process.env.RAVEN_TUI_CHECK === '1') {
  process.exit(0)
}

const socketPath = process.env.RAVEN_RPC_SOCKET

if (!socketPath) {
  process.stderr.write('raven-tui: RAVEN_RPC_SOCKET env var required; spawn via `raven tui` parent\n')
  process.exit(2)
}











const gwCompat = new GatewayClientCompat({ socketPath })
const gw = gwCompat as unknown as GatewayClient



await gw.start()

const dumpNotice = (snap: MemorySnapshot, dump: HeapDumpResult | null) =>
  `raven-tui: ${snap.level} memory (${formatBytes(snap.heapUsed)}) — auto heap dump → ${dump?.heapPath ?? '(failed)'}\n`

setupGracefulExit({
  cleanups: [
    () => {
      resetTerminalModes()

      return gw.kill()
    }
  ],
  onError: (scope, err) => {
    const message = err instanceof Error ? `${err.name}: ${err.message}` : String(err)

    process.stderr.write(`raven-tui ${scope}: ${message.slice(0, 2000)}\n`)
  },
  onSignal: signal => {
    resetTerminalModes()
    process.stderr.write(`raven-tui: received ${signal}\n`)
  }
})

const stopMemoryMonitor = startMemoryMonitor({
  onCritical: (snap, dump) => {
    resetTerminalModes()
    process.stderr.write(dumpNotice(snap, dump))
    process.stderr.write('raven-tui: exiting to avoid OOM; restart to recover\n')
    process.exit(137)
  },
  onHigh: (snap, dump) => process.stderr.write(dumpNotice(snap, dump))
})

if (process.env.RAVEN_HEAPDUMP_ON_START === '1') {
  void performHeapDump('manual')
}

process.on('beforeExit', () => stopMemoryMonitor())

const [ink, { App }, { logFrameEvent }, { trackFrame }] = await Promise.all([
  import('@hermes/ink'),
  import('./app.js'),
  import('./lib/perfPane.js'),
  import('./lib/fpsStore.js')
])



const onFrame =
  logFrameEvent || trackFrame
    ? (event: FrameEvent) => {
        logFrameEvent?.(event)
        trackFrame?.(event.durationMs)
      }
    : undefined








if (process.env.RAVEN_TUI_CHECK === '1') {
  setTimeout(() => {
    try {
      void gw.kill()
    } finally {
      resetTerminalModes()
      process.exit(0)
    }
  }, 100)
} else {
  ink.render(<App gw={gw} rpcClient={gwCompat.rpcClient} />, { exitOnCtrlC: false, onFrame })
}
