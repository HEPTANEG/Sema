interface SetupOptions {
  cleanups?: (() => Promise<void> | void)[]
  failsafeMs?: number
  onError?: (scope: 'uncaughtException' | 'unhandledRejection', err: unknown) => void
  onSignal?: (signal: NodeJS.Signals) => void
}

const SIGNAL_EXIT_CODE: Record<'SIGHUP' | 'SIGINT' | 'SIGTERM', number> = {
  SIGHUP: 129,
  SIGINT: 130,
  SIGTERM: 143
}

let wired = false
let deferrals = 0
let pendingSignal: keyof typeof SIGNAL_EXIT_CODE | undefined
let replaySignal: ((code: number, signal: NodeJS.Signals) => void) | undefined








export function deferSignalExit(): () => void {
  deferrals += 1
  let released = false

  return () => {
    if (released) {
      return
    }

    released = true
    deferrals = Math.max(0, deferrals - 1)

    if (deferrals === 0 && pendingSignal) {
      const sig = pendingSignal
      pendingSignal = undefined
      replaySignal?.(SIGNAL_EXIT_CODE[sig], sig)
    }
  }
}

export function setupGracefulExit({ cleanups = [], failsafeMs = 4000, onError, onSignal }: SetupOptions = {}) {
  if (wired) {
    return
  }

  wired = true

  let shuttingDown = false

  const exit = (code: number, signal?: NodeJS.Signals) => {
    if (shuttingDown) {
      return
    }

    shuttingDown = true

    if (signal) {
      onSignal?.(signal)
    }

    setTimeout(() => process.exit(code), failsafeMs).unref?.()

    void Promise.allSettled(cleanups.map(fn => Promise.resolve().then(fn))).finally(() => process.exit(code))
  }

  replaySignal = exit

  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP'] as const) {
    process.on(sig, () => {
      if (deferrals > 0) {



        pendingSignal ??= sig
        return
      }

      exit(SIGNAL_EXIT_CODE[sig], sig)
    })
  }

  process.on('uncaughtException', err => onError?.('uncaughtException', err))
  process.on('unhandledRejection', reason => onError?.('unhandledRejection', reason))
}
