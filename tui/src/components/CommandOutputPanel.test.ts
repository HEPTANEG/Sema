import {describe, expect, it} from 'vitest';
import {commandOutputPageSize, wrapCommandOutput} from './CommandOutputPanel.js';

describe('CommandOutputPanel layout helpers', () => {
  it('counts wrapped terminal rows rather than only newline-delimited rows', () => {
    expect(wrapCommandOutput('abcdefghij', 4)).toEqual(['abcd', 'efgh', 'ij']);
    expect(wrapCommandOutput('第一行\n第二行', 20)).toEqual(['第一行', '第二行']);
  });

  it('uses the same compact height as the root command overlay', () => {
    expect(commandOutputPageSize(24)).toBe(6);
    expect(commandOutputPageSize(60)).toBe(10);
  });
});
