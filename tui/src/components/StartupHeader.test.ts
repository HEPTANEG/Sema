import {describe, expect, it} from 'vitest';
import {formatStartupPermissions} from './StartupHeader.js';

describe('StartupHeader helpers', () => {
  it('summarizes default permissions', () => {
    expect(formatStartupPermissions({})).toBe('default');
    expect(formatStartupPermissions({allow_all: false, allow_edits: false, allow_run_commands: false})).toBe('default');
  });

  it('summarizes enabled temporary permissions', () => {
    expect(formatStartupPermissions({allow_edits: true})).toBe('edits');
    expect(formatStartupPermissions({allow_run_commands: true})).toBe('run commands');
    expect(formatStartupPermissions({allow_edits: true, allow_run_commands: true})).toBe('edits, run commands');
  });

  it('gives unattended priority', () => {
    expect(formatStartupPermissions({unattended: true, allow_all: true, allow_edits: true, allow_run_commands: true})).toBe('unattended');
    expect(formatStartupPermissions({allow_all: true, allow_edits: true, allow_run_commands: true})).toBe('unattended');
  });
});
