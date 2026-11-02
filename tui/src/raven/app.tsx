import { activeColorTier, oscColor, type StdinProps, useStdin } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import type { ChatStreamRpcClient } from './app/chatStream.js'
import type { GatewayClient } from './gatewayClientStub.js'

import { GatewayProvider } from './app/gatewayContext.js'
import { $uiState, $uiTheme, applyTerminalBackground } from './app/uiStore.js'
import { useMainApp } from './app/useMainApp.js'
import { AppLayout } from './components/appLayout.js'
import { cursorColorHex } from './theme.js'







function useTerminalBackgroundProbe() {




  const { querier } = useStdin() as StdinProps

  useEffect(() => {
    if (!querier) {
      return
    }

    let cancelled = false

    void Promise.all([querier.send(oscColor(11)), querier.flush()]).then(([reply]) => {
      if (!cancelled && reply) {
        applyTerminalBackground(reply.data)
      }
    })

    return () => {
      cancelled = true
    }
  }, [querier])
}







const setCursorColorSeq = (hex: string) => `]12;${hex}`
const RESET_CURSOR_COLOR_SEQ = ']112'

function useHardwareCursorColor() {
  const hex = cursorColorHex(useStore($uiTheme))

  useEffect(() => {

    if (activeColorTier() === 0) {
      return
    }

    process.stdout.write(setCursorColorSeq(hex))
  }, [hex])



  useEffect(
    () => () => {
      if (activeColorTier() !== 0) {
        process.stdout.write(RESET_CURSOR_COLOR_SEQ)
      }
    },
    []
  )
}

export function App({ gw, rpcClient }: { gw: GatewayClient; rpcClient?: ChatStreamRpcClient }) {
  const { appActions, appComposer, appProgress, appStatus, appTranscript, gateway } = useMainApp(gw, rpcClient)
  const { mouseTracking } = useStore($uiState)

  useTerminalBackgroundProbe()
  useHardwareCursorColor()

  return (
    <GatewayProvider value={gateway}>
      <AppLayout
        actions={appActions}
        composer={appComposer}
        mouseTracking={mouseTracking}
        progress={appProgress}
        status={appStatus}
        transcript={appTranscript}
      />
    </GatewayProvider>
  )
}
