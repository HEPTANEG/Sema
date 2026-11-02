import React, {useEffect, useState} from 'react';
import {Box, Text} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {$backgroundActivities, $foregroundStatus, $liveItems} from '../stores/runtime.js';
import {formatToolName} from '../stores/runtime_formatting.js';
import {theme} from '../theme.js';

const SPINNER_FRAMES = ['◐', '◓', '◑', '◒'];

export function ProcessStatusBar() {
  const foregroundStatus = useStore($foregroundStatus);
  const backgroundActivities = useStore($backgroundActivities);
  const liveItems = useStore($liveItems);
  const [frame, setFrame] = useState(0);
  const statuses = formatProcessStatuses(foregroundStatus, backgroundActivities, liveItems);

  useEffect(() => {
    if (!statuses.length) {
      return;
    }
    const timer = setInterval(() => setFrame(current => (current + 1) % SPINNER_FRAMES.length), 180);
    return () => clearInterval(timer);
  }, [statuses.length]);

  if (!statuses.length) {
    return null;
  }

  return (
    <Box height={1} flexDirection="row" flexShrink={0} paddingX={1}>
      <Text color={theme.accentBright}>{SPINNER_FRAMES[frame]} </Text>
      <Text color={theme.accentBright} wrap="truncate-end">{statuses.join(' | ')}</Text>
    </Box>
  );
}

export function formatProcessStatuses(
  foregroundStatus: string,
  backgroundActivities: Array<{label: string}>,
  liveItems: Array<{role: string; tool?: {status?: string; tool?: string}}>
) {
  const statuses: string[] = [];
  const foreground = foregroundStatus.trim().replace(/\.+$/, '');
  if (foreground) {
    statuses.push(foreground);
  }
  for (const item of liveItems) {
    if (item.role === 'tool' && item.tool?.status === 'running' && item.tool.tool) {
      statuses.push(`Running ${formatToolName(item.tool.tool)}`);
    }
  }
  for (const activity of backgroundActivities) {
    if (activity.label.trim()) {
      statuses.push(activity.label.trim());
    }
  }
  return statuses;
}
