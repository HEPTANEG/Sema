import {describe, expect, it} from 'vitest';
import {matchingSlashCommands, resolveSlashCommandInput} from './slash.js';

describe('matchingSlashCommands', () => {
  it('matches slash commands by prefix', () => {
    expect(
      matchingSlashCommands(
        [
          {name: '/help', help: 'help'},
          {name: '/status', help: 'status'}
        ],
        '/h'
      )
    ).toEqual([{name: '/help', help: 'help'}]);
  });

  it('resolves the first displayed completion on submit', () => {
    const commands = [
      {name: '/memory', help: 'memory'},
      {name: '/memory status', help: 'memory status'},
    ];

    expect(matchingSlashCommands(commands, '/m')).toEqual([
      {name: '/memory', help: 'memory'},
      {name: '/memory status', help: 'memory status'},
    ]);
    expect(resolveSlashCommandInput(commands, '/m')).toBe('/memory');
    expect(resolveSlashCommandInput(commands, '/mem')).toBe('/memory');
  });

  it('keeps arguments when completing a command', () => {
    const commands = [
    ];

  });

  it('completes a partially typed final token of a multi-word command', () => {
    const commands = [
      {name: '/memory budget status', help: 'status'},
      {name: '/memory budget set', help: 'set budget'},
      {name: '/memory budget reset', help: 'reset budget'}
    ];

    expect(matchingSlashCommands(commands, '/memory budget re')).toEqual([
      {name: '/memory budget reset', help: 'reset budget'}
    ]);
    expect(resolveSlashCommandInput(commands, '/memory budget re')).toBe('/memory budget reset');
  });

  it('resolves a small typo when the intended command is unambiguous', () => {
    const commands = [
      {name: '/status', help: 'status'},
    ];

    expect(matchingSlashCommands(commands, '/stauts')).toEqual([{name: '/status', help: 'status'}]);
    expect(resolveSlashCommandInput(commands, '/stauts')).toBe('/status');
  });

  it('does not resolve unknown multi-token commands to shorter commands', () => {
    const commands = [
      {name: '/memory', help: 'memory'},
      {name: '/memory status', help: 'memory status'},
      {name: '/memory context status', help: 'context'},
    ];

    expect(matchingSlashCommands(commands, '/memory missing')).toEqual([]);
    expect(resolveSlashCommandInput(commands, '/memory missing')).toBe('/memory missing');
  });

  it('keeps unknown slash input unchanged', () => {
    const commands = [{name: '/status', help: 'status'}];
    expect(resolveSlashCommandInput(commands, '/completely-unknown')).toBe('/completely-unknown');
  });
});
