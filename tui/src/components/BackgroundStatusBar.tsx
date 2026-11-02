import React, {useEffect, useState} from 'react';
import {Box, Text} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {
  $backgroundActivities,
  $contextPressure,
  $lastTurnDurationMs,
  $turnCodeChanges,
  $turnStartedAt,
  type BackgroundActivity
} from '../stores/runtime.js';
import type {ContextPressureTelemetry, EditStats} from '../protocol/types.js';
import {theme} from '../theme.js';

export function BackgroundStatusBar() {
  const activities = useStore($backgroundActivities);
  const context = useStore($contextPressure);
  const lastTurnDurationMs = useStore($lastTurnDurationMs);
  const turnStartedAt = useStore($turnStartedAt);
  const changes = useStore($turnCodeChanges);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (turnStartedAt === null) {
      return;
    }
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [turnStartedAt]);

  const displayedDuration = turnStartedAt === null
    ? lastTurnDurationMs
    : Math.max(0, now - turnStartedAt);
  const line = formatStatusBar({activities, changes, context, lastTurnDurationMs: displayedDuration});

  return (
    <Box height={1} flexDirection="row" flexShrink={0} paddingX={1}>
      <Text color={theme.mutedBorder}>
        {'─ '}
      </Text>
      <Text color={theme.mutedBorder} wrap="truncate-end">
        {line.left}
      </Text>
    </Box>
  );
}

export function formatStatusBar({
  activities,
  changes,
  context,
  lastTurnDurationMs = null
}: {
  activities: BackgroundActivity[];
  changes: EditStats;
  context: ContextPressureTelemetry | null;
  lastTurnDurationMs?: number | null;
}) {
  const hasChanges = hasCodeChanges(changes);
  const leftParts = [formatContextBudgets(context || {})];
  if (hasChanges) leftParts.push(`+${changes.added} -${changes.removed}`);
  if (lastTurnDurationMs !== null && lastTurnDurationMs !== undefined) {
    leftParts.push(`Last response: ${formatTurnDuration(lastTurnDurationMs)}`);
  }

  return {left: leftParts.join(' | ')};
}

export function formatTurnDuration(durationMs: number) {
  const seconds = Math.max(0, durationMs) / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}m ${String(remainder).padStart(2, '0')}s`;
}

export function formatContextBudgets(context: ContextPressureTelemetry) {
  return `STM ${formatBudget(context.stm, context.stm_max)} | WM ${formatBudget(context.wm, context.wm_max)}`;
}

function formatBudget(value?: number, maximum?: number) {
  if (value === undefined && maximum === undefined) return 'n/a';
  if (maximum === undefined || maximum <= 0) return formatTokenCount(value);
  return `${formatTokenCount(value)}/${formatTokenCount(maximum)}`;
}

function formatTokenCount(value?: number) {
  if (value === undefined || !Number.isFinite(value)) return 'n/a';
  const rounded = Math.max(0, Math.round(value));
  if (rounded >= 1000) {
    const compact = rounded / 1000;
    return `${compact >= 10 ? compact.toFixed(0) : compact.toFixed(1)}k`;
  }
  return String(rounded);
}

export function formatBackgroundActivities(activities: BackgroundActivity[]) {
  return activities.map(activity => activity.label).join(' | ');
}

export function formatForegroundStatus(status: string) {
  return status.trim().replace(/\.+$/, '');
}

export function backgroundStatusStyle(activities: BackgroundActivity[], changes: EditStats = {added: 0, removed: 0, files: []}, foregroundStatus = '') {
  if (!activities.length && !hasCodeChanges(changes) && !foregroundStatus.trim()) {
    return {
      borderColor: theme.mutedBorder,
      markerColor: theme.mutedBorder,
      dimColor: true,
      bold: false
    };
  }

  return {
    borderColor: 'blueBright',
    markerColor: 'blueBright',
    dimColor: false,
    bold: true
  };
}

export function hasCodeChanges(changes: EditStats) {
  return Number(changes.added || 0) > 0 || Number(changes.removed || 0) > 0;
}

export function formatCodeChanges(changes: EditStats) {
  if (!hasCodeChanges(changes)) {
    return '';
  }
  const fileSuffix = changes.files?.length ? `  ${changes.files.length} file${changes.files.length === 1 ? '' : 's'}` : '';
  return `Changes: +${changes.added} -${changes.removed}${fileSuffix}`;
}
