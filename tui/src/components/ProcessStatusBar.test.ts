import {describe, expect, it} from 'vitest';
import {formatProcessStatuses} from './ProcessStatusBar.js';

describe('ProcessStatusBar helpers', () => {
  it('combines the current stage, running tool, and existing background activities', () => {
    expect(formatProcessStatuses(
      'Finalizing context...',
      [{label: 'Topic organizing'}],
      [{role: 'tool', tool: {status: 'running', tool: 'web_search'}}]
    )).toEqual(['Finalizing context', 'Running Web Search', 'Topic organizing']);
  });

  it('does not retain completed tools or render an idle process row', () => {
    expect(formatProcessStatuses('', [], [
      {role: 'tool', tool: {status: 'ok', tool: 'web_search'}}
    ])).toEqual([]);
  });
});
