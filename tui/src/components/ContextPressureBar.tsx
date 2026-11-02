import React, {useEffect, useState} from 'react';
import {Text} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {$contextPressure} from '../stores/runtime.js';
import type {ContextPressureTelemetry} from '../protocol/types.js';
import {GutterBlock} from './GutterBlock.js';

export function ContextPressureBar() {
  const telemetry = useStore($contextPressure);
  const [highlighted, setHighlighted] = useState(false);

  useEffect(() => {
    if (!telemetry) {
      setHighlighted(false);
      return;
    }
    setHighlighted(true);
    const timer = setTimeout(() => setHighlighted(false), 5000);
    return () => clearTimeout(timer);
  }, [telemetry]);

  if (!telemetry) {
    return null;
  }
  const style = contextPressureStyle(highlighted);

  return (
    <GutterBlock kind="system" dim={style.dim} bold={style.bold} markerColor={style.markerColor} marginBottom={1}>
      <Text color={style.textColor} dimColor={style.dim} bold={style.bold} wrap="truncate-end">{formatContextPressure(telemetry)}</Text>
    </GutterBlock>
  );
}

export function formatContextPressure(telemetry: ContextPressureTelemetry) {
  const stm = formatBudget(telemetry.stm, telemetry.stm_max);
  const wm = formatBudget(telemetry.wm, telemetry.wm_max);
  const scores = [
    `I ${formatScore(telemetry.I)}`,
    `S ${formatScore(telemetry.S)}`,
    `R ${formatScore(telemetry.R)}`,
    `L ${formatScore(telemetry.L)}`
  ];
  if (telemetry.P_context !== undefined) {
    scores.push(`P ${formatScore(telemetry.P_context)}`);
  }
  return `STM ${stm} | WM ${wm} | ISRL ${scores.join(' ')}`;
}

export function contextPressureStyle(highlighted: boolean) {
  if (highlighted) {
    return {
      dim: false,
      bold: true,
      markerColor: 'blueBright',
      textColor: 'blueBright'
    };
  }
  return {
    dim: true,
    bold: false,
    markerColor: undefined,
    textColor: undefined
  };
}

export function formatBudget(value?: number, maximum?: number) {
  if (value === undefined && maximum === undefined) {
    return 'n/a';
  }
  if (maximum === undefined || maximum <= 0) {
    return formatTokenCount(value);
  }
  return `${formatTokenCount(value)}/${formatTokenCount(maximum)}`;
}

export function formatTokenCount(value?: number) {
  if (value === undefined || !Number.isFinite(value)) {
    return 'n/a';
  }
  const rounded = Math.max(0, Math.round(value));
  if (rounded >= 1000) {
    const compact = rounded / 1000;
    return `${compact >= 10 ? compact.toFixed(0) : compact.toFixed(1)}k`;
  }
  return String(rounded);
}

export function formatScore(value?: number) {
  return value === undefined || !Number.isFinite(value) ? 'n/a' : value.toFixed(2);
}
