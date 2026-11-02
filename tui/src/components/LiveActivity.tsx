import React, {useEffect, useState} from 'react';
import {Box, Text} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {
  $busy,
  $config,
  $lastError,
  $lastEventAt,
  $operationStartedAt,
  $pendingConfirmation,
  $status
} from '../stores/runtime.js';
import {GutterBlock} from './GutterBlock.js';

export function LiveActivity() {
  const busy = useStore($busy);
  const status = useStore($status);
  const error = useStore($lastError);
  const startedAt = useStore($operationStartedAt);
  const lastEventAt = useStore($lastEventAt);
  const pendingConfirmation = useStore($pendingConfirmation);
  const config = useStore($config);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!busy || pendingConfirmation || config) {
      return;
    }
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [busy, pendingConfirmation, config]);

  if (!busy || pendingConfirmation || config || error) {
    return null;
  }
  if (!shouldShowLiveActivity(status)) {
    return null;
  }

  const elapsed = startedAt ? Math.max(0, Math.floor((now - startedAt) / 1000)) : 0;
  const idle = lastEventAt ? Math.max(0, Math.floor((now - lastEventAt) / 1000)) : null;

  return (
    <Box flexDirection="column" marginBottom={1}>
      <GutterBlock kind="system" dim>
      <Text>
        {humanStatus(status)}
        {elapsed ? ` ${elapsed}s` : ''}
        {idle !== null ? ` | last event ${idle}s ago` : ''}
        {' | press Ctrl+X to cancel'}
      </Text>
      </GutterBlock>
    </Box>
  );
}

function shouldShowLiveActivity(status: string) {
  return (
    status === 'cancelling' ||
    status.startsWith('model retry')
  );
}

function humanStatus(status: string) {
  if (status === 'cancelling') {
    return 'cancelling...';
  }
  if (status.startsWith('model retry')) {
    return `${status}...`;
  }
  return `${status}...`;
}
