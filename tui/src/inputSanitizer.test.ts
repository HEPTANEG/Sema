import {describe, expect, it} from 'vitest';
import {stripTerminalControlInput} from './inputSanitizer.js';

describe('stripTerminalControlInput', () => {
  it('removes terminal focus tracking escape sequences', () => {
    expect(stripTerminalControlInput('hello\u001b[O\u001b[I world')).toBe('hello world');
  });

  it('removes focus tracking fragments when the escape byte was already consumed', () => {
    expect(stripTerminalControlInput('[O[I[O[Ihello[O')).toBe('hello');
  });

  it('keeps normal bracketed text that is not a focus report fragment', () => {
    expect(stripTerminalControlInput('check [OK] and [INFO]')).toBe('check [OK] and [INFO]');
  });

  it('removes bracketed paste markers', () => {
    expect(stripTerminalControlInput('\u001b[200~line one\nline two\u001b[201~')).toBe('line one\nline two');
    expect(stripTerminalControlInput('[200~line one\nline two[201~')).toBe('line one\nline two');
  });

  it('removes a mouse sequence if a terminal parser fragment reaches the composer', () => {
    expect(stripTerminalControlInput('before[<64;37;19Mafter')).toBe('beforeafter');
  });
});
