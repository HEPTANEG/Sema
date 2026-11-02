import {describe, expect, it} from 'vitest';
import {formatPreview, isConfirmationSubmitKey} from './ConfirmationModal.js';

describe('ConfirmationModal helpers', () => {
  it('formats the preview window without separator lines', () => {
    const preview = Array.from({length: 20}, (_, index) => `line ${index + 1}`).join('\n');
    const tail = formatPreview(preview, 0).split('\n');

    expect(tail).toContain('line 20');
    expect(tail).not.toContain('line 1');
    expect(formatPreview(preview, 16)).toContain('line 1');
  });

  it('accepts normalized and raw terminal Enter input', () => {
    expect(isConfirmationSubmitKey('', {return: true})).toBe(true);
    expect(isConfirmationSubmitKey('\r', {})).toBe(true);
    expect(isConfirmationSubmitKey('\n', {})).toBe(true);
    expect(isConfirmationSubmitKey(' ', {})).toBe(false);
  });
});
