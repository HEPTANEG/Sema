import {describe, expect, it} from 'vitest';
import {hasActiveLiveItem, messageMarkerColor, messageMarkerForRole, transcriptTextStyle} from './Transcript.js';
import type {TranscriptItem} from '../stores/runtime.js';

describe('Transcript live region helpers', () => {
  it('uses Raven-style markers while keeping Sema user color semantics', () => {
    expect(messageMarkerForRole('user')).toBe('❯');
    expect(messageMarkerForRole('assistant')).toBe('▸');
    expect(messageMarkerColor('user')).toBe('cyanBright');
    expect(messageMarkerColor('assistant')).toBe('white');
  });

  it('detects active assistant stages and running tools', () => {
    const stage: TranscriptItem = {id: 'stage', role: 'assistant', content: 'Sema is reading your intent...', stage: true};
    const runningTool: TranscriptItem = {
      id: 'tool',
      role: 'tool',
      content: 'running Web Search',
      tool: {id: 'tool', turn_id: 'turn', tool: 'web_search', args: {}, status: 'running', result: ''}
    };

    expect(hasActiveLiveItem([stage])).toBe(true);
    expect(hasActiveLiveItem([runningTool])).toBe(true);
  });

  it('treats completed log items as inactive', () => {
    const assistant: TranscriptItem = {id: 'assistant', role: 'assistant', content: 'Done.', markdown: true};
    const tool: TranscriptItem = {
      id: 'tool',
      role: 'tool',
      content: 'ok Web Search',
      tool: {id: 'tool', turn_id: 'turn', tool: 'web_search', args: {}, status: 'ok', result: 'result'}
    };

    expect(hasActiveLiveItem([assistant, tool])).toBe(false);
  });

  it('keeps streaming assistant text stable until Markdown is finalized', () => {
    expect(transcriptTextStyle({role: 'assistant', stage: false})).toEqual({bold: false, dim: false});
    expect(transcriptTextStyle({role: 'assistant', stage: true})).toEqual({bold: false, dim: true});
    expect(transcriptTextStyle({role: 'user', stage: false})).toEqual({bold: true, dim: false});
  });
});
