import React from 'react';
import {Box, Text} from '@hermes/ink';
import {theme} from '../theme.js';

export type MarkdownBlock =
  | {type: 'blank'; text: ''}
  | {type: 'heading'; level: number; text: string}
  | {type: 'blockquote'; text: string}
  | {type: 'list'; ordered: boolean; marker: string; text: string}
  | {type: 'code'; language: string; text: string}
  | {type: 'table'; headers: string[]; rows: string[][]}
  | {type: 'paragraph'; text: string};

type InlineSpan = {
  type: 'text' | 'bold' | 'code' | 'link';
  text: string;
  url?: string;
};

export function MarkdownText({content}: {content: string}) {
  return (
    <Box flexDirection="column">
      {parseMarkdown(content).map((block, index) => (
        <MarkdownBlockView key={`${block.type}-${index}`} block={block} />
      ))}
    </Box>
  );
}

function MarkdownBlockView({block}: {block: MarkdownBlock}) {
  if (block.type === 'blank') {
    return <Text color="white"> </Text>;
  }
  if (block.type === 'heading') {
    return (
      <Text bold color={block.level === 1 ? theme.accentBright : theme.accent}>
        {renderInlineSpans(block.text)}
      </Text>
    );
  }
  if (block.type === 'blockquote') {
    return (
      <Text color="white" dimColor>
        <Text color={theme.accent}>{'░ '}</Text>
        {renderInlineSpans(block.text)}
      </Text>
    );
  }
  if (block.type === 'list') {
    return (
      <Text color="white">
        {'  '}
        <Text color={theme.accent} dimColor>{block.marker}</Text>
        {' '}
        {renderInlineSpans(block.text)}
      </Text>
    );
  }
  if (block.type === 'code') {
    const prefix = block.language ? ` ${block.language} ` : '';
    return (
      <Box flexDirection="column" marginLeft={2}>
        {prefix ? <Text color={theme.accent} dimColor>{prefix}</Text> : null}
        <Text color="white">{block.text || ' '}</Text>
      </Box>
    );
  }
  if (block.type === 'table') {
    return <MarkdownTable block={block} />;
  }
  return <Text color="white">{renderInlineSpans(block.text)}</Text>;
}

function MarkdownTable({block}: {block: Extract<MarkdownBlock, {type: 'table'}>}) {
  const widths = tableWidths(block);
  const header = formatTableRow(block.headers, widths);
  const divider = widths.map(width => '-'.repeat(width)).join('  ');
  return (
    <Box flexDirection="column">
      <Text color={theme.accent} bold>{header}</Text>
      <Text dimColor>{divider}</Text>
      {block.rows.map((row, index) => (
        <Text key={index} color="white">{formatTableRow(row, widths)}</Text>
      ))}
    </Box>
  );
}

export function parseMarkdown(content: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  let inCode = false;
  let codeLanguage = '';
  let codeLines: string[] = [];

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const fence = line.match(/^```(.*)$/);
    if (fence) {
      if (inCode) {
        blocks.push({type: 'code', language: codeLanguage, text: codeLines.join('\n')});
        inCode = false;
        codeLanguage = '';
        codeLines = [];
      } else {
        inCode = true;
        codeLanguage = fence[1].trim();
        codeLines = [];
      }
      continue;
    }

    if (inCode) {
      codeLines.push(line);
      continue;
    }

    const table = tryParseTable(lines, index);
    if (table) {
      blocks.push(table.block);
      index = table.nextIndex - 1;
      continue;
    }

    if (!line.trim()) {
      blocks.push({type: 'blank', text: ''});
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      blocks.push({type: 'heading', level: heading[1].length, text: heading[2]});
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      blocks.push({type: 'blockquote', text: quote[1]});
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (unordered) {
      blocks.push({type: 'list', ordered: false, marker: '-', text: unordered[1]});
      continue;
    }

    const ordered = line.match(/^\s*(\d+[.)])\s+(.+)$/);
    if (ordered) {
      blocks.push({type: 'list', ordered: true, marker: ordered[1], text: ordered[2]});
      continue;
    }

    blocks.push({type: 'paragraph', text: line});
  }

  if (inCode) {
    blocks.push({type: 'code', language: codeLanguage, text: codeLines.join('\n')});
  }

  return blocks;
}

export function parseInlineMarkdown(text: string): InlineSpan[] {
  const spans: InlineSpan[] = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\[[^\]]+\]\([^)]+\))/g;
  let lastIndex = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > lastIndex) {
      spans.push({type: 'text', text: text.slice(lastIndex, match.index)});
    }
    const token = match[0];
    if (token.startsWith('`')) {
      spans.push({type: 'code', text: token.slice(1, -1)});
    } else if (token.startsWith('**') || token.startsWith('__')) {
      spans.push({type: 'bold', text: token.slice(2, -2)});
    } else {
      const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (link) {
        spans.push({type: 'link', text: link[1], url: link[2]});
      }
    }
    lastIndex = match.index + token.length;
  }
  if (lastIndex < text.length) {
    spans.push({type: 'text', text: text.slice(lastIndex)});
  }
  return spans.length ? spans : [{type: 'text', text}];
}

function renderInlineSpans(text: string) {
  return parseInlineMarkdown(text).map((span, index) => {
    if (span.type === 'bold') {
      return <Text key={index} bold>{span.text}</Text>;
    }
    if (span.type === 'code') {
      return <Text key={index} color={theme.accent}>{span.text}</Text>;
    }
    if (span.type === 'link') {
      return (
        <Text key={index}>
          {span.text}
          <Text color={theme.accent} dimColor>{` (${span.url})`}</Text>
        </Text>
      );
    }
    return <Text key={index}>{stripItalic(span.text)}</Text>;
  });
}

function tryParseTable(lines: string[], index: number) {
  const header = splitTableLine(lines[index]);
  const divider = splitTableLine(lines[index + 1] || '');
  if (!header || !divider || !divider.every(cell => /^:?-{3,}:?$/.test(cell.trim()))) {
    return null;
  }
  const rows: string[][] = [];
  let nextIndex = index + 2;
  while (nextIndex < lines.length) {
    const row = splitTableLine(lines[nextIndex]);
    if (!row) {
      break;
    }
    rows.push(row);
    nextIndex += 1;
  }
  return {block: {type: 'table' as const, headers: header, rows}, nextIndex};
}

function splitTableLine(line: string) {
  const trimmed = line.trim();
  if (!trimmed.includes('|')) {
    return null;
  }
  const withoutEdges = trimmed.replace(/^\|/, '').replace(/\|$/, '');
  const cells = withoutEdges.split('|').map(cell => stripInlinePlain(cell.trim()));
  return cells.length >= 2 ? cells : null;
}

function tableWidths(block: Extract<MarkdownBlock, {type: 'table'}>) {
  return block.headers.map((header, index) => {
    const rowWidths = block.rows.map(row => displayWidth(row[index] || ''));
    return Math.max(displayWidth(header), ...rowWidths, 3);
  });
}

function formatTableRow(row: string[], widths: number[]) {
  return widths.map((width, index) => padCell(row[index] || '', width)).join('  ');
}

function padCell(text: string, width: number) {
  return `${text}${' '.repeat(Math.max(0, width - displayWidth(text)))}`;
}

function displayWidth(text: string) {
  return Array.from(text).reduce((width, char) => width + (char.charCodeAt(0) > 255 ? 2 : 1), 0);
}

function stripInlinePlain(text: string) {
  return text
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1 ($2)')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/_([^_]+)_/g, '$1');
}

function stripItalic(text: string) {
  return text.replace(/\*([^*]+)\*/g, '$1').replace(/_([^_]+)_/g, '$1');
}
