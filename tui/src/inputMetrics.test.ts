import {describe, expect, it} from 'vitest';
import {composerPromptWidth, cursorLayout, displayWidth, inputVisualHeight, offsetFromPosition, stableComposerColumns} from './inputMetrics.js';

describe('Raven-style input metrics', () => {
  it('measures CJK and emoji using terminal cells', () => {
    expect(displayWidth('测')).toBe(2);
    expect(displayWidth('🙂')).toBe(2);
    expect(displayWidth('a')).toBe(1);
  });

  it('keeps cursor layout stable across wrapping and newlines', () => {
    expect(cursorLayout('abcdef', 6, 4)).toEqual({line: 1, column: 2});
    expect(cursorLayout('你好\nworld', 4, 4)).toEqual({line: 1, column: 1});
    expect(inputVisualHeight('abcdef', 4)).toBe(2);
  });

  it('maps a visual cell back to a grapheme boundary', () => {
    expect(offsetFromPosition('你好abc', 0, 0, 8)).toBe(0);
    expect(offsetFromPosition('你好abc', 0, 2, 8)).toBe(1);
    expect(offsetFromPosition('你好abc', 0, 4, 8)).toBe(2);
  });

  it('reserves stable composer columns for the prompt and frame', () => {
    const promptWidth = composerPromptWidth('❯');
    expect(promptWidth).toBeGreaterThan(1);
    expect(stableComposerColumns(80, promptWidth)).toBe(74);
    expect(stableComposerColumns(20, promptWidth)).toBe(16);
  });
});
