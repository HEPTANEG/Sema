import React from 'react';
import {Box, Text} from '@hermes/ink';
import {theme} from '../theme.js';

export type GutterKind = 'user' | 'assistant' | 'tool' | 'status' | 'important' | 'system';

type GutterBlockProps = {
  kind: GutterKind;
  children: React.ReactNode;
  indent?: number;
  dim?: boolean;
  bold?: boolean;
  markerColor?: string;
  marginBottom?: number;
};

const gutterWidth = 3;

export function GutterBlock({kind, children, indent = 0, dim = false, bold = false, markerColor, marginBottom = 1}: GutterBlockProps) {
  const symbol = gutterSymbol(kind);
  const color = markerColor || gutterColor(kind);

  return (
    <Box flexDirection="row" marginLeft={indent} marginBottom={marginBottom}>
      <Box width={gutterWidth}>
        <Text color={color} dimColor={dim} bold={bold}>
          {symbol}
        </Text>
      </Box>
      <Box flexDirection="column" flexShrink={1}>
        {children}
      </Box>
    </Box>
  );
}

export function gutterSymbol(kind: GutterKind) {
  if (kind === 'status') {
    return '▒';
  }
  if (kind === 'tool') {
    return '●';
  }
  if (kind === 'important') {
    return '█';
  }
  if (kind === 'system') {
    return '░';
  }
  return '▓';
}

export function gutterColor(kind: GutterKind) {
  if (kind === 'user') {
    return theme.accentBright;
  }
  if (kind === 'status') {
    return 'blueBright';
  }
  if (kind === 'important') {
    return theme.danger;
  }
  if (kind === 'system' || kind === 'tool') {
    return theme.mutedBorder;
  }
  return undefined;
}
