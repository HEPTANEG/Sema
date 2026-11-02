import React, {useEffect, useMemo} from 'react';
import {AlternateScreen, Box, useApp, useStdout} from '@hermes/ink';
import {BridgeClient} from './protocol/client.js';
import {enableBracketedPaste, enableTerminalFocusTracking} from './notifications.js';
import {
  $activeView,
  $config,
  $lastError,
  $pendingConfirmation,
  $terminalColumns,
  $terminalRows,
  applyBridgeEvent,
  applyInitialize,
  failRequest
} from './stores/runtime.js';
import {TranscriptViewport} from './components/Transcript.js';
import {StartupHeader} from './components/StartupHeader.js';
import {InputBox} from './components/InputBox.js';
import {ConfirmationModal} from './components/ConfirmationModal.js';
import {ConfigPanel, configOverlayHeight} from './components/ConfigPanel.js';
import {SettingsPanel} from './components/SettingsPanel.js';
import {CommandOutputPanel} from './components/CommandOutputPanel.js';
import {BackgroundStatusBar} from './components/BackgroundStatusBar.js';
import {ProcessStatusBar} from './components/ProcessStatusBar.js';
import {InputRouter} from './components/InputRouter.js';
import {useStore} from '@nanostores/react';

export function App() {
  const {exit} = useApp();
  const {stdout} = useStdout();
  const client = useMemo(() => new BridgeClient(), []);
  const terminalRows = useStore($terminalRows);
  const activeView = useStore($activeView);
  const config = useStore($config);
  const confirmation = useStore($pendingConfirmation);
  const overlayOpen = activeView !== 'chat' || Boolean(config || confirmation);
  const overlayHeight = overlayOpen
    ? activeView === 'config'
      ? configOverlayHeight(terminalRows)
      : activeView === 'settings'
        ? configOverlayHeight(terminalRows)
      : Math.max(4, Math.min(12, Math.floor(terminalRows * 0.35)))
    : 0;

  useEffect(() => {
    if (!stdout) {
      return;
    }
    const updateColumns = () => {
      $terminalColumns.set(Math.max(20, stdout.columns || 80));
      $terminalRows.set(Math.max(12, stdout.rows || 24));
    };
    stdout.on('resize', updateColumns);
    updateColumns();
    return () => {
      stdout.off('resize', updateColumns);
    };
  }, [stdout]);


  useEffect(() => {
    const disableFocusTracking = enableTerminalFocusTracking();
    const disableBracketedPaste = enableBracketedPaste();
    client.on('event', applyBridgeEvent);
    client.on('malformed', event => {
      $lastError.set(`Malformed backend message: ${event.line}`);
    });
    client.on('stderr', chunk => {
      $lastError.set(String(chunk));
    });
    client.on('request_error', error => {
      failRequest(error);
    });
    client.on('exit', () => {
      exit();
    });
    void client.initialize().then(applyInitialize).catch(error => {
      $lastError.set(error instanceof Error ? error.message : String(error));
    });
    return () => {
      disableBracketedPaste();
      disableFocusTracking();
      client.dispose();
    };
  }, [client, exit]);

  return (
    <AlternateScreen mouseTracking>
      <Box height={terminalRows} flexDirection="column" overflow="hidden">
        <Box flexShrink={0} overflow="hidden">
          <StartupHeader compact={terminalRows < 30} />
        </Box>

        <Box flexGrow={1} flexShrink={1} minHeight={1} overflow="hidden">
          <TranscriptViewport />
        </Box>

        {overlayOpen ? (
          <Box height={overlayHeight} flexShrink={0} overflow="hidden">
            <CommandOutputPanel client={client} />
            <ConfigPanel client={client} />
            <SettingsPanel client={client} />
            <ConfirmationModal client={client} />
          </Box>
        ) : null}

        <Box flexDirection="column" flexShrink={0}>
          <InputBox client={client} maxLines={6} />
          <BackgroundStatusBar />
          <ProcessStatusBar />
        </Box>

        <InputRouter />
      </Box>
    </AlternateScreen>
  );
}
