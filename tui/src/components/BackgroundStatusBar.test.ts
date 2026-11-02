import {describe, expect, it} from 'vitest';
import {backgroundStatusStyle, formatBackgroundActivities, formatCodeChanges, formatContextBudgets, formatForegroundStatus, formatStatusBar, formatTurnDuration, hasCodeChanges} from './BackgroundStatusBar.js';

describe('BackgroundStatusBar helpers', () => {
  it('uses empty activity text when no background work is active', () => {
    expect(formatBackgroundActivities([])).toBe('');
  });

  it('formats multiple background activities without a permanent prefix', () => {
    expect(
      formatBackgroundActivities([
        {id: 'background-one', kind: 'background', label: 'Background one', startedAt: 1},
        {id: 'background-two', kind: 'background', label: 'Background two', startedAt: 2}
      ])
    ).toBe('Background one | Background two');
  });

  it('uses dim styling when idle and blue highlight when active', () => {
    expect(backgroundStatusStyle([], {added: 0, removed: 0, files: []})).toMatchObject({
      borderColor: 'gray',
      markerColor: 'gray',
      dimColor: true,
      bold: false
    });
    expect(backgroundStatusStyle([{id: 'background', kind: 'background', label: 'Background work', startedAt: 2}], {added: 0, removed: 0, files: []})).toMatchObject({
      borderColor: 'blueBright',
      markerColor: 'blueBright',
      dimColor: false,
      bold: true
    });
    expect(backgroundStatusStyle([], {added: 1, removed: 0, files: ['a.ts']})).toMatchObject({
      borderColor: 'blueBright',
      markerColor: 'blueBright',
      dimColor: false,
      bold: true
    });
    expect(backgroundStatusStyle([], {added: 0, removed: 0, files: []}, 'Sema is reading your intent...')).toMatchObject({
      borderColor: 'blueBright',
      markerColor: 'blueBright',
      dimColor: false,
      bold: true
    });
  });

  it('formats code changes inside the unified status bar', () => {
    expect(hasCodeChanges({added: 0, removed: 0, files: []})).toBe(false);
    expect(formatCodeChanges({added: 0, removed: 0, files: []})).toBe('');
    expect(formatCodeChanges({added: 105, removed: 90, files: ['a.ts', 'b.ts']})).toBe('Changes: +105 -90  2 files');
    expect(formatCodeChanges({added: 1, removed: 0, files: ['a.ts']})).toBe('Changes: +1 -0  1 file');
  });

  it('formats foreground status without trailing ellipses', () => {
    expect(formatForegroundStatus('Sema is reading your intent...')).toBe('Sema is reading your intent');
  });

  it('keeps a one-line status rule present while idle', () => {
    const result = formatStatusBar({
      activities: [],
      changes: {added: 0, removed: 0, files: []},
      context: null
    });
    expect(result.left).toContain('STM n/a | WM n/a');
    expect(result.left).not.toContain('deepseek');
    expect(result.left).not.toContain('C:\\Users');
  });

  it('keeps only STM and WM budgets from context telemetry', () => {
    const result = formatStatusBar({
      activities: [],
      changes: {added: 0, removed: 0, files: []},
      context: {stm: 13000, stm_max: 50000, wm: 13000, wm_max: 50000, I: 0.08, S: 0.03, R: 0, L: 0, P_context: 0.03}
    });
    expect(formatContextBudgets({stm: 13000, stm_max: 50000, wm: 13000, wm_max: 50000})).toBe('STM 13k/50k | WM 13k/50k');
    expect(result.left).toContain('STM 13k/50k | WM 13k/50k');
    expect(result.left).not.toContain('ISRL');
    expect(result.left).not.toContain('P ');
  });

  it('keeps persistent metrics separate from process status', () => {
    const result = formatStatusBar({
      activities: [{id: 'topic', kind: 'background', label: 'Topic organizing', startedAt: 1}],
      changes: {added: 105, removed: 90, files: []},
      context: null
    });
    expect(result.left).toContain('STM n/a | WM n/a');
    expect(result.left).toContain('+105 -90');
    expect(result.left).not.toContain('Topic organizing');
    expect(result.left).not.toContain('deepseek');
  });

  it('formats the latest response duration for the persistent status bar', () => {
    expect(formatTurnDuration(2250)).toBe('2.3s');
    expect(formatTurnDuration(12500)).toBe('13s');
    expect(formatTurnDuration(75400)).toBe('1m 15s');
    expect(formatStatusBar({
      activities: [],
      changes: {added: 0, removed: 0, files: []},
      context: null,
      lastTurnDurationMs: 2250
    }).left).toContain('Last response: 2.3s');
  });
});
