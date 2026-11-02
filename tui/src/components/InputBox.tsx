import React, {useState} from 'react';
import {Box, Text, useSelection} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {
  $activeView, $busy, $cancelRequested, $config, $pendingConfirmation, $slashCommands, $terminalColumns,
  cancelTurn, openLatestToolDetail, submitPrompt
} from '../stores/runtime.js';
import {matchingSlashCommands, resolveSlashCommandInput} from '../commands/slash.js';
import {TextInput} from './TextInput.js';
import {stripTerminalControlInput} from '../inputSanitizer.js';
import type {BridgeClient} from '../protocol/client.js';
import {theme} from '../theme.js';

export function InputBox({client, maxLines = 6}: {client: BridgeClient; maxLines?: number}) {
  const [value, setValue] = useState('');
  const commands = useStore($slashCommands);
  const pendingConfirmation = useStore($pendingConfirmation);
  const config = useStore($config);
  const busy = useStore($busy);
  const activeView = useStore($activeView);
  const cancelRequested = useStore($cancelRequested);
  const terminalColumns = useStore($terminalColumns);
  const selection = useSelection();
  const matches = matchingSlashCommands(commands, value);
  const panelNeedsInput = activeView === 'confirm' || activeView === 'config';
  const inputEnabled = !panelNeedsInput && !pendingConfirmation && !config && !busy;

  async function submit(text: string) {
    const prompt = resolveSlashCommandInput(commands, normalizePromptInput(text));
    setValue('');
    const result = await submitPrompt(client, prompt);
    if (result.shouldExit) {
      process.exit(0);
    }
  }

  function handleShortcut(input: string, key: {ctrl?: boolean}) {
    if (key.ctrl && input.toLowerCase() === 'c') {
      if (selection.hasSelection()) {
        void selection.copySelection();
        return true;
      }
      if (value) {
        setValue('');
        return true;
      }
      if (busy && !pendingConfirmation && !config && !cancelRequested) {
        void cancelTurn(client);
        return true;
      }


      return true;
    }
    if (inputEnabled && key.ctrl && input.toLowerCase() === 'u') {
      setValue('');
      return true;
    }
    if (inputEnabled && isOpenLatestToolShortcut(input, key)) {
      openLatestToolDetail();
      return true;
    }
    if (busy && !pendingConfirmation && !config && !cancelRequested && isCancelTurnShortcut(input, key)) {
      void cancelTurn(client);
      return true;
    }
    return false;
  }

  return (
    <Box flexDirection="column" flexShrink={0}>
      {matches.length > 0 && inputEnabled ? (
        <Box flexDirection="column">
          {matches.map(command => (
            <Text key={command.name} dimColor>
              {command.name} {command.help}
            </Text>
          ))}
        </Box>
      ) : null}
      <Box
        flexDirection="row"
        flexShrink={0}
        borderStyle="single"
        borderColor={inputEnabled ? theme.accent : theme.mutedBorder}
        paddingX={1}
      >
        <Text color={inputEnabled ? theme.accentBright : theme.mutedBorder} bold>{'❯ '}</Text>
        <Box flexGrow={1} flexShrink={1}>
        <TextInput
          value={value}
          columns={Math.max(12, terminalColumns - 5)}
          maxLines={Math.max(1, maxLines)}
          focus={inputEnabled}
          disabled={!inputEnabled}
          onChange={next => setValue(stripTerminalControlInput(normalizePromptInput(next)))}
          onSubmit={submit}
          onShortcut={handleShortcut}
        />
        </Box>
      </Box>
    </Box>
  );
}

export function normalizePromptInput(value: string) {
  return stripTerminalControlInput(value)
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .replace(/\t/g, ' ');
}

export function deleteLastInputCharacter(value: string) {
  return Array.from(value).slice(0, -1).join('');
}

export function isOpenLatestToolShortcut(input: string, key: {ctrl?: boolean}) {
  return Boolean(key.ctrl && input.toLowerCase() === 't');
}

export function isCancelTurnShortcut(input: string, key: {ctrl?: boolean}) {
  return Boolean(key.ctrl && input.toLowerCase() === 'x');
}

export function formatPromptPreview(value: string, columns: number, maxLines = 6) {
  if (!value) {
    return [];
  }
  const width = Math.max(12, Math.min(columns - 2, 96));
  const wrapped = wrapPromptLines(value, width);
  if (wrapped.length > maxLines) {
    const visibleLineCount = Math.max(1, maxLines - 1);
    const hiddenCount = wrapped.length - visibleLineCount;
    const visible = wrapped.slice(-visibleLineCount);
    return [`... ${hiddenCount} earlier line${hiddenCount === 1 ? '' : 's'}`, ...visible];
  }
  return wrapped;
}

function wrapPromptLines(value: string, width: number) {
  const lines: string[] = [];
  for (const rawLine of value.split('\n')) {
    if (!rawLine) {
      lines.push('');
      continue;
    }
    let remaining = rawLine;
    while (remaining.length > width) {
      lines.push(remaining.slice(0, width));
      remaining = remaining.slice(width);
    }
    lines.push(remaining);
  }
  return lines;
}
