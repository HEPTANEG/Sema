import React, {useEffect, useState} from 'react';
import {Box, Text, useInput} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {$pendingConfirmation, respondToPendingConfirmation} from '../stores/runtime.js';
import {stripTerminalControlInput} from '../inputSanitizer.js';
import {GutterBlock} from './GutterBlock.js';
import type {BridgeClient} from '../protocol/client.js';
import {TextInput} from './TextInput.js';

const previewWindowLines = 16;

export function ConfirmationModal({client}: {client: BridgeClient}) {
  const confirmation = useStore($pendingConfirmation);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [reasonChoice, setReasonChoice] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [previewScroll, setPreviewScroll] = useState(0);
  const [showPreview, setShowPreview] = useState(false);

  useEffect(() => {
    setSelectedIndex(0);
    setReasonChoice(null);
    setReason('');
    setPreviewScroll(0);
    setShowPreview(false);
  }, [confirmation?.confirmation_id]);

  useInput((input, key) => {
    if (!confirmation) {
      return;
    }
    if (reasonChoice) {
      if (key.escape) {
        setReasonChoice(null);
        setReason('');
      }
      return;
    }
    if (key.pageUp) {
      setPreviewScroll(offset => offset + previewWindowLines);
      return;
    }
    if (key.pageDown) {
      setPreviewScroll(offset => Math.max(0, offset - previewWindowLines));
      return;
    }
    if (key.upArrow) {
      setSelectedIndex(index => Math.max(0, index - 1));
      return;
    }
    if (key.downArrow) {
      setSelectedIndex(index => Math.min(confirmation.choices.length - 1, index + 1));
      return;
    }
    if (isConfirmationSubmitKey(input, key)) {
      const choice = confirmation.choices[selectedIndex];
      if (choice) {
        submitChoice(client, choice, setReasonChoice, setShowPreview);
      }
      return;
    }
    if (key.escape) {
      void respondToPendingConfirmation(client, 'no', 'Cancelled from TUI.');
      return;
    }
    const index = Number(input) - 1;
    if (Number.isInteger(index) && confirmation.choices[index]) {
      submitChoice(client, confirmation.choices[index], setReasonChoice, setShowPreview);
    }
  }, {isActive: Boolean(confirmation)});

  if (!confirmation) {
    return null;
  }
  const preview = confirmation.preview && showPreview ? formatPreview(confirmation.preview, previewScroll) : '';

  return (
    <GutterBlock kind="important" markerColor="red">
      <Text color="red">{confirmation.title}</Text>
      <Text>{confirmation.details}</Text>
      <Box flexDirection="column" marginTop={1}>
        {confirmation.choices.map((choice, index) => (
          <Text key={choice.id} color={index === selectedIndex ? 'red' : undefined}>
            {index === selectedIndex ? '█ ' : '  '}
            {index + 1}. {choice.label}
            {choice.requires_reason ? ' (reason required)' : ''}
          </Text>
        ))}
        <Text dimColor>Use Up/Down and Enter, or press number. Esc rejects.</Text>
      </Box>
      {reasonChoice ? (
        <Box marginTop={1}>
          <Text color="yellow">Reason: </Text>
          <TextInput
            columns={64}
            value={reason}
            onChange={value => setReason(stripTerminalControlInput(value))}
            onSubmit={value => void respondToPendingConfirmation(client, reasonChoice, stripTerminalControlInput(value))}
            focus
          />
        </Box>
      ) : null}
      {confirmation.preview ? (
        <Box flexDirection="column" marginTop={1}>
          {showPreview ? (
            <>
              <Text color="yellow">Preview</Text>
              <Text>{preview}</Text>
              <Text dimColor>Use PgUp/PgDn to scroll preview.</Text>
            </>
          ) : (
            <Text dimColor>Choose Preview modification to inspect the full change.</Text>
          )}
        </Box>
      ) : null}
    </GutterBlock>
  );
}

export function isConfirmationSubmitKey(input: string, key: {return?: boolean}) {


  return Boolean(key.return || input === '\r' || input === '\n');
}

function submitChoice(
  client: BridgeClient,
  choice: {id: string; requires_reason?: boolean; local_preview?: boolean},
  setReasonChoice: (choice: string) => void,
  setShowPreview: React.Dispatch<React.SetStateAction<boolean>>
) {
  if (choice.local_preview || choice.id === 'preview') {
    setShowPreview(value => !value);
    return;
  }
  if (choice.requires_reason) {
    setReasonChoice(choice.id);
    return;
  }
  void respondToPendingConfirmation(client, choice.id);
}

export function formatPreview(preview: string, scroll: number) {
  const lines = preview.split('\n');
  const maxOffset = Math.max(0, lines.length - previewWindowLines);
  const offset = Math.max(0, Math.min(scroll, maxOffset));
  const end = lines.length - offset;
  const start = Math.max(0, end - previewWindowLines);
  return lines.slice(start, end).join('\n');
}
