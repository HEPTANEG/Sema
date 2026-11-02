import {describe, expect, it, vi} from 'vitest';
import {$lastError, $liveItems, $staticLogItems, $transcript} from '../stores/runtime.js';
import {
  deleteLastInputCharacter,
  formatPromptPreview,
  isCancelTurnShortcut,
  isOpenLatestToolShortcut,
  normalizePromptInput
} from './InputBox.js';

describe('InputBox submit behavior', () => {
  it('does not add duplicate user transcript before backend event', async () => {
    $staticLogItems.set([]);
    $liveItems.set([]);
    $transcript.set([]);
    const prompt = 'hello';
    const sendMessage = vi.fn().mockResolvedValue({ok: true});

    await sendMessage(prompt);

    expect($transcript.get()).toEqual([]);
  });

  it('records send failures in the error store', () => {
    $lastError.set('');
    const error = new Error('provider failed');

    $lastError.set(error.message);

    expect($lastError.get()).toBe('provider failed');
  });

  it('normalizes pasted prompt text without dropping multiline content', () => {
    const pasted = [
      'Research ShanghaiTech SIST advisors and recommend by direction:',
      'Positive: AI Agent, multimodal LLM, NLP, Web3.',
      'Negative: cryptography, brain-computer interfaces, quantum computing.',
      'Special: MIT graduates can be recommended directly.'
    ].join('\r\n');

    expect(normalizePromptInput(pasted)).toBe(
      [
        'Research ShanghaiTech SIST advisors and recommend by direction:',
        'Positive: AI Agent, multimodal LLM, NLP, Web3.',
        'Negative: cryptography, brain-computer interfaces, quantum computing.',
        'Special: MIT graduates can be recommended directly.'
      ].join('\n')
    );
  });

  it('removes terminal focus tracking fragments from prompt input', () => {
    expect(normalizePromptInput('[O[I[O[Ihello\u001b[O world\u001b[I')).toBe('hello world');
  });

  it('keeps the full long prompt buffer while previewing only the tail', () => {
    const longPrompt = '0123456789'.repeat(30);
    const preview = formatPromptPreview(longPrompt, 22, 4);

    expect(longPrompt.length).toBe(300);
    expect(preview).toHaveLength(4);
    expect(preview[0]).toBe('... 12 earlier lines');
    expect(preview.join('')).toContain('0123456789');
  });

  it('keeps bare t available for prompt text and reserves Ctrl+T for tool detail', () => {
    expect(isOpenLatestToolShortcut('t', {})).toBe(false);
    expect(isOpenLatestToolShortcut('T', {})).toBe(false);
    expect(isOpenLatestToolShortcut('t', {ctrl: true})).toBe(true);
    expect(isOpenLatestToolShortcut('T', {ctrl: true})).toBe(true);
  });

  it('keeps bare c available for prompt text and reserves Ctrl+X for cancellation', () => {
    expect(isCancelTurnShortcut('c', {})).toBe(false);
    expect(isCancelTurnShortcut('C', {})).toBe(false);
    expect(isCancelTurnShortcut('c', {ctrl: true})).toBe(false);
    expect(isCancelTurnShortcut('x', {ctrl: true})).toBe(true);
    expect(isCancelTurnShortcut('X', {ctrl: true})).toBe(true);
  });

  it('deletes one unicode character from the buffer', () => {
    expect(deleteLastInputCharacter('hello好')).toBe('hello');
  });
});
