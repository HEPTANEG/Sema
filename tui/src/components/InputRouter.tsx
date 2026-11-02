import React from 'react';
import {useInput} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {$activeView, $config, $pendingConfirmation, scrollTranscriptBy, scrollTranscriptTo} from '../stores/runtime.js';

export function InputRouter() {
  const activeView = useStore($activeView);
  const config = useStore($config);
  const confirmation = useStore($pendingConfirmation);
  const active = activeView === 'chat' && !config && !confirmation;

  useInput((_input, key) => {
    if (key.wheelUp) {
      scrollTranscriptBy(-4);
      return;
    }
    if (key.wheelDown) {
      scrollTranscriptBy(4);
      return;
    }
    if (key.pageUp) {
      scrollTranscriptBy(-8);
      return;
    }
    if (key.pageDown) {
      scrollTranscriptBy(8);
      return;
    }
    if (key.ctrl && isHomeKey(_input)) {
      scrollTranscriptTo('top');
      return;
    }
    if (key.ctrl && isEndKey(_input)) {
      scrollTranscriptTo('bottom');
    }
  }, {isActive: active});

  return null;
}

function isHomeKey(input: string) {
  return input === '\u001b[H' || input === '\u001b[1~' || input === '\u001bOH';
}

function isEndKey(input: string) {
  return input === '\u001b[F' || input === '\u001b[4~' || input === '\u001bOF';
}
