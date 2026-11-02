import React, {useEffect, useMemo, useRef, useState} from 'react';
import {Box, Text, useDeclaredCursor, useInput, useStdout} from '@hermes/ink';
import {cursorLayout, graphemes, graphemeWidth} from '../inputMetrics.js';
import {stripTerminalControlInput} from '../inputSanitizer.js';

type Key = {backspace?: boolean; delete?: boolean; downArrow?: boolean; end?: boolean; home?: boolean; leftArrow?: boolean; return?: boolean; rightArrow?: boolean; shift?: boolean; upArrow?: boolean; ctrl?: boolean; pageUp?: boolean; pageDown?: boolean};

type TextInputProps = {
  secret?: boolean;
  value: string;
  columns?: number;
  maxLines?: number;
  focus?: boolean;
  disabled?: boolean;
  onChange: (value: string) => void;
  onSubmit?: (value: string) => void;
  onShortcut?: (input: string, key: Key) => boolean;
};

export function TextInput({value, columns = 80, maxLines = 6, focus = true, disabled = false, secret = false, onChange, onSubmit, onShortcut}: TextInputProps) {
  const [cursor, setCursor] = useState(value.length);
  const valueRef = useRef(value);
  const cursorRef = useRef(cursor);
  const selfEditRef = useRef(false);
  cursorRef.current = cursor;
  const {stdout} = useStdout();
  const nativeCursor = Boolean(stdout?.isTTY && focus && !disabled);






  const renderedValue = selfEditRef.current ? valueRef.current : value;

  useEffect(() => {
    if (selfEditRef.current) {
      selfEditRef.current = false;
      return;
    }
    valueRef.current = value;
    setCursor(previous => Math.min(previous, value.length));
    cursorRef.current = Math.min(cursorRef.current, value.length);
  }, [value]);

  const width = Math.max(8, columns - 2);
  const lines = useMemo(() => wrapInput(renderedValue, width), [renderedValue, width]);
  const cursorLocation = locateCursor(lines, cursor);
  const firstVisibleLine = Math.max(0, Math.min(cursorLocation.line, lines.length - maxLines));
  const visibleLines = lines.slice(firstVisibleLine, firstVisibleLine + maxLines);
  const ravenCursor = cursorLayout(renderedValue, cursor, width);
  const declaredCursorRef = useDeclaredCursor({
    line: Math.max(0, ravenCursor.line - firstVisibleLine),
    column: ravenCursor.column,
    active: nativeCursor
  });

  useInput((input, key) => {
    if (onShortcut?.(input, key)) {
      return;
    }
    if (disabled || !focus) {
      return;
    }
    const current = valueRef.current;
    const position = cursorRef.current;
    if (key.return) {
      if (key.shift) {
        insert('\n');
      } else {
        onSubmit?.(current);
      }
      return;
    }
    if (key.leftArrow) {
      setPosition(previousGraphemeBoundary(current, position));
      return;
    }
    if (key.rightArrow) {
      setPosition(nextGraphemeBoundary(current, position));
      return;
    }
    if (isHomeKey(input)) {
      setPosition(lineStart(current, position));
      return;
    }
    if (isEndKey(input)) {
      setPosition(lineEnd(current, position));
      return;
    }
    if (key.upArrow || key.downArrow) {
      setPosition(moveVertical(current, position, key.upArrow ? -1 : 1, width));
      return;
    }
    if (key.backspace) {
      const start = previousGraphemeBoundary(current, position);
      replace(start, position, '');
      return;
    }
    if (key.delete) {
      replace(position, nextGraphemeBoundary(current, position), '');
      return;
    }
    const nextInput = stripTerminalControlInput(input)
      .replace(/\r\n/g, '\n')
      .replace(/\r/g, '\n')
      .replace(/\t/g, ' ');
    if (nextInput) {
      insert(nextInput);
    }
  }, {isActive: focus});

  function setPosition(next: number) {
    const bounded = Math.max(0, Math.min(valueRef.current.length, next));
    setCursor(bounded);
    cursorRef.current = bounded;
  }

  function replace(start: number, end: number, inserted: string) {
    const next = `${valueRef.current.slice(0, start)}${inserted}${valueRef.current.slice(end)}`;



    valueRef.current = next;
    selfEditRef.current = true;
    onChange(next);
    setPosition(start + inserted.length);
  }

  function insert(inserted: string) {
    replace(cursorRef.current, cursorRef.current, inserted);
  }

  return (
    <Box ref={declaredCursorRef} width={width} flexDirection="column">
      <Text color="cyanBright" wrap="wrap">
        {secret ? '*'.repeat(Math.min(renderedValue.length, width)) : renderVisibleInput(visibleLines, nativeCursor && focus && !disabled)}
      </Text>
    </Box>
  );
}

function isHomeKey(input: string) {
  return input === '\u001b[H' || input === '\u001b[1~' || input === '\u001bOH';
}

function isEndKey(input: string) {
  return input === '\u001b[F' || input === '\u001b[4~' || input === '\u001bOF';
}

type InputLine = {text: string; start: number; end: number};

function wrapInput(value: string, width: number): InputLine[] {
  const result: InputLine[] = [];
  let offset = 0;
  for (const raw of value.split('\n')) {
    const parts = graphemes(raw);
    if (!parts.length) {
      result.push({text: '', start: offset, end: offset});
      offset += 1;
      continue;
    }
    let line = '';
    let start = offset;
    let lineWidth = 0;
    for (const part of parts) {
      const partWidth = graphemeWidth(part);
      if (line && lineWidth + partWidth > width) {
        result.push({text: line, start, end: offset});
        line = '';
        lineWidth = 0;
        start = offset;
      }
      line += part;
      lineWidth += partWidth;
      offset += part.length;
    }
    result.push({text: line, start, end: offset});
    offset += 1;
  }
  return result.length ? result : [{text: '', start: 0, end: 0}];
}

function locateCursor(lines: InputLine[], cursor: number) {
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (cursor <= line.end || index === lines.length - 1) {
      const prefix = line.text.slice(0, Math.max(0, cursor - line.start));
      return {line: index, column: prefix ? graphemes(prefix).reduce((sum, part) => sum + graphemeWidth(part), 0) : 0};
    }
  }
  return {line: 0, column: 0};
}

function renderVisibleInput(lines: InputLine[], nativeCursor: boolean) {
  const text = lines.map(line => line.text).join('\n');
  if (nativeCursor) {



    return `${text}\u00a0`;
  }
  return text || ' ';
}

function previousGraphemeBoundary(value: string, position: number) {
  const prefix = value.slice(0, position);
  const parts = graphemes(prefix);
  return position - (parts.at(-1)?.length || 0);
}

function nextGraphemeBoundary(value: string, position: number) {
  const parts = graphemes(value.slice(position));
  return position + (parts[0]?.length || 0);
}

function lineStart(value: string, position: number) {
  const before = value.slice(0, position);
  const index = before.lastIndexOf('\n');
  return index < 0 ? 0 : index + 1;
}

function lineEnd(value: string, position: number) {
  const index = value.indexOf('\n', position);
  return index < 0 ? value.length : index;
}

function moveVertical(value: string, position: number, direction: number, width: number) {
  const lines = wrapInput(value, width);
  const current = locateCursor(lines, position);
  const target = Math.max(0, Math.min(lines.length - 1, current.line + direction));
  const targetLine = lines[target];
  let targetColumn = 0;
  let targetOffset = targetLine.start;
  for (const part of graphemes(targetLine.text)) {
    const partWidth = graphemeWidth(part);
    if (targetColumn + partWidth > current.column) {
      break;
    }
    targetColumn += partWidth;
    targetOffset += part.length;
  }
  return targetOffset;
}
