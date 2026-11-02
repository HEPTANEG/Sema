import {describe, expect, it} from 'vitest';
import {contextPressureStyle, formatBudget, formatContextPressure, formatScore, formatTokenCount} from './ContextPressureBar.js';

describe('ContextPressureBar helpers', () => {
  it('formats context pressure telemetry compactly', () => {
    expect(
      formatContextPressure({
        stm: 11726,
        stm_max: 50000,
        wm: 10600,
        wm_max: 50000,
        I: 0.21,
        S: 0.38,
        R: 0,
        L: 0.44,
        P_context: 0.27
      })
    ).toBe('STM 12k/50k | WM 11k/50k | ISRL I 0.21 S 0.38 R 0.00 L 0.44 P 0.27');
  });

  it('formats missing and small values safely', () => {
    expect(formatBudget(undefined, undefined)).toBe('n/a');
    expect(formatBudget(900, 10000)).toBe('900/10k');
    expect(formatTokenCount(1234)).toBe('1.2k');
    expect(formatScore(undefined)).toBe('n/a');
  });

  it('uses a stronger style while telemetry is freshly updated', () => {
    expect(contextPressureStyle(true)).toMatchObject({
      dim: false,
      bold: true,
      markerColor: 'blueBright',
      textColor: 'blueBright'
    });
    expect(contextPressureStyle(false)).toMatchObject({
      dim: true,
      bold: false
    });
  });
});
