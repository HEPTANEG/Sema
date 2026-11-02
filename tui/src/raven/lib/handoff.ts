



import type { RunExternalProcess } from '@hermes/ink'

import { withInkSuspended } from '@hermes/ink'

import { deferSignalExit } from './gracefulExit.js'










export async function suspendForHandoff(run: RunExternalProcess): Promise<void> {
  const restore = deferSignalExit()

  try {
    await withInkSuspended(run)
  } finally {
    restore()
  }
}
