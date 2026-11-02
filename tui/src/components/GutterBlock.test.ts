import {describe, expect, it} from 'vitest';
import {gutterColor, gutterSymbol} from './GutterBlock.js';

describe('GutterBlock helpers', () => {
  it('maps conversation roles to the shared message marker', () => {
    expect(gutterSymbol('user')).toBe('\u2593');
    expect(gutterSymbol('assistant')).toBe('\u2593');
  });

  it('maps status, tool, important, and system roles to distinct markers', () => {
    expect(gutterSymbol('status')).toBe('\u2592');
    expect(gutterSymbol('tool')).toBe('\u25cf');
    expect(gutterSymbol('important')).toBe('\u2588');
    expect(gutterSymbol('system')).toBe('\u2591');
  });

  it('uses color to distinguish message and status semantics', () => {
    expect(gutterColor('user')).toBe('cyanBright');
    expect(gutterColor('assistant')).toBeUndefined();
    expect(gutterColor('status')).toBe('blueBright');
    expect(gutterColor('important')).toBe('red');
  });
});
