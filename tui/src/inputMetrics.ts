import {stringWidth} from '@hermes/ink';

const segmenter = typeof Intl !== 'undefined' && 'Segmenter' in Intl
  ? new Intl.Segmenter(undefined, {granularity: 'grapheme'})
  : null;

export function graphemes(value: string) {
  if (!segmenter) {
    return Array.from(value);
  }
  return Array.from(segmenter.segment(value), part => part.segment);
}

export function graphemeWidth(value: string) {
  return value ? Math.max(0, stringWidth(value)) : 0;
}

export function wrapText(value: string, width: number) {
  const safeWidth = Math.max(1, width);
  const lines: string[] = [];
  for (const rawLine of value.split('\n')) {
    const parts = graphemes(rawLine);
    if (!parts.length) {
      lines.push('');
      continue;
    }
    let line = '';
    let lineWidth = 0;
    for (const part of parts) {
      const partWidth = graphemeWidth(part);
      if (line && lineWidth + partWidth > safeWidth) {
        lines.push(line);
        line = '';
        lineWidth = 0;
      }
      line += part;
      lineWidth += partWidth;
    }
    lines.push(line);
  }
  return lines;
}

export function displayWidth(value: string) {
  return graphemes(value).reduce((width, part) => width + graphemeWidth(part), 0);
}

export const COMPOSER_PROMPT_GAP_WIDTH = 1;

type VisualLine = {start: number; end: number};

function visualLines(value: string, columns: number): VisualLine[] {
  const width = Math.max(1, columns);
  const lines: VisualLine[] = [];
  let sourceStart = 0;

  for (const sourceLine of value.split('\n')) {
    const parts = graphemes(sourceLine);
    if (!parts.length) {
      lines.push({start: sourceStart, end: sourceStart});
      sourceStart += 1;
      continue;
    }

    let lineStartPart = 0;
    let lineStart = sourceStart;
    let column = 0;
    let offset = sourceStart;
    let i = 0;
    while (i < parts.length) {
      const part = parts[i]!;
      const partStart = offset;
      const partWidth = graphemeWidth(part);
      if (column + partWidth > width && i > lineStartPart) {
        lines.push({start: lineStart, end: partStart});
        lineStartPart = i;
        lineStart = partStart;
        column = 0;
        continue;
      }
      column += partWidth;
      offset += part.length;
      i += 1;
    }
    lines.push({start: lineStart, end: sourceStart + sourceLine.length});
    sourceStart += sourceLine.length + 1;
  }
  return lines.length ? lines : [{start: 0, end: 0}];
}

function widthBetween(value: string, start: number, end: number) {
  return displayWidth(value.slice(start, end));
}

export function cursorLayout(value: string, cursor: number, columns: number) {
  const position = Math.max(0, Math.min(cursor, value.length));
  const width = Math.max(1, columns);
  const lines = visualLines(value, width);
  let lineIndex = 0;
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i]!.start <= position) lineIndex = i;
    else break;
  }
  const line = lines[lineIndex]!;
  let column = widthBetween(value, line.start, Math.min(position, line.end));
  if (column >= width) {
    lineIndex += 1;
    column = 0;
  }
  return {line: lineIndex, column};
}

export function offsetFromPosition(value: string, row: number, column: number, columns: number) {
  if (!value.length) return 0;
  const lines = visualLines(value, columns);
  const target = lines[Math.max(0, Math.min(lines.length - 1, Math.floor(row)))]!;
  const targetColumn = Math.max(0, Math.floor(column));
  let current = 0;
  let offset = target.start;
  for (const part of graphemes(value.slice(target.start, target.end))) {
    const width = graphemeWidth(part);
    if (targetColumn <= current + Math.max(0, width - 1)) return offset;
    current += width;
    offset += part.length;
  }
  return target.end;
}

export function inputVisualHeight(value: string, columns: number) {
  return cursorLayout(value, value.length, columns).line + 1;
}

export function composerPromptWidth(prompt: string) {
  return Math.max(1, stringWidth(prompt)) + COMPOSER_PROMPT_GAP_WIDTH;
}

export function stableComposerColumns(totalColumns: number, promptWidth: number) {
  return Math.max(1, totalColumns - promptWidth - 2 - (totalColumns - promptWidth >= 24 ? 2 : 0));
}
