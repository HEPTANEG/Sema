import React, {useEffect} from 'react';
import {Box, Text, useInput} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {
  $activeView,
  $commandOutput,
  $panelScroll,
  $terminalColumns,
  $terminalRows,
  closeActiveView
} from '../stores/runtime.js';
import type {BridgeClient} from '../protocol/client.js';
import {theme} from '../theme.js';
import {GutterBlock} from './GutterBlock.js';
import {wrapText} from '../inputMetrics.js';

export function CommandOutputPanel({client}: {client: BridgeClient}) {
  void client;
  const activeView = useStore($activeView);
  const output = useStore($commandOutput);
  const panelScroll = useStore($panelScroll);
  const terminalColumns = useStore($terminalColumns);
  const terminalRows = useStore($terminalRows);

  const lines = output ? wrapCommandOutput(output.content, Math.max(1, terminalColumns - 4)) : [];
  const pageSize = commandOutputPageSize(terminalRows);
  const maxScroll = Math.max(0, lines.length - pageSize);

  useEffect(() => {
    if (panelScroll > maxScroll) {
      $panelScroll.set(maxScroll);
    }
  }, [maxScroll, panelScroll]);

  useInput((_input, key) => {
    if (key.escape) {
      closeActiveView();
      return;
    }
    if (key.upArrow || key.pageUp) {
      $panelScroll.set(Math.max(0, panelScroll - (key.pageUp ? pageSize : 1)));
      return;
    }
    if (key.downArrow || key.pageDown) {
      $panelScroll.set(Math.min(maxScroll, panelScroll + (key.pageDown ? pageSize : 1)));
      return;
    }
  }, {isActive: activeView === 'commandOutput' && Boolean(output)});

  if (activeView !== 'commandOutput' || !output) {
    return null;
  }

  return (
    <GutterBlock kind="system">
      <Text color={theme.info}>{output.title}</Text>
      <Box flexDirection="column" height={pageSize} overflow="hidden">
        {lines.slice(panelScroll, panelScroll + pageSize).map((line, index) => (
          <Text key={`${panelScroll + index}-${line}`} wrap="wrap">{line || ' '}</Text>
        ))}
      </Box>
      <Text dimColor>
        {lines.length > pageSize ? `Lines ${panelScroll + 1}-${Math.min(lines.length, panelScroll + pageSize)} of ${lines.length}. ` : ''}
        Up/Down or PgUp/PgDn scroll. Esc closes.
      </Text>
    </GutterBlock>
  );
}

export function commandOutputPageSize(terminalRows: number) {
  const overlayHeight = Math.max(4, Math.min(12, Math.floor(terminalRows * 0.35)));
  return Math.max(1, overlayHeight - 2);
}

export function wrapCommandOutput(content: string, width: number) {
  return wrapText(content, Math.max(1, width));
}
