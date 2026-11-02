import {afterEach, describe, expect, it, vi} from 'vitest';
import {commitSettingsActions} from './SettingsPanel.js';
import {$slashCommands, closeActiveView} from '../stores/runtime.js';
import {matchingSlashCommands} from '../commands/slash.js';
import type {BridgeClient} from '../protocol/client.js';
import {firstSelectableIndex, formatActionValue, formatCurrentActionValue, isSelectableAction, nextSelectableIndex, readSettingsCategories, settingsActionWindowSize} from './SettingsPanel.js';

describe('SettingsPanel helpers', () => {
  afterEach(() => {$slashCommands.set([]); closeActiveView();});

  it('refreshes autocomplete from developer enable and disable responses', async () => {
    const publicCommands = [{name: '/memory', help: 'Memory'}];
    const developerCommands = [...publicCommands, {name: '/memory ltm visualize', help: 'Visualize'}];
    $slashCommands.set(publicCommands);
    const updateSettings = vi.fn()
      .mockResolvedValueOnce({settings: {categories: []}, slash_commands: developerCommands})
      .mockResolvedValueOnce({settings: {categories: []}, slash_commands: publicCommands});
    const executeSlash = vi.fn();
    const client = {updateSettings, executeSlash} as unknown as BridgeClient;
    await commitSettingsActions(client, ['developer.enable']);
    expect($slashCommands.get()).toEqual(developerCommands);
    expect(matchingSlashCommands($slashCommands.get(), '/memory ltm visualize')[0].name).toBe('/memory ltm visualize');
    await commitSettingsActions(client, ['developer.disable']);
    expect($slashCommands.get()).toEqual(publicCommands);
    expect(executeSlash).not.toHaveBeenCalled();
  });

  it('does not change the command list when saving fails', async () => {
    const commands = [{name: '/memory', help: 'Memory'}];
    $slashCommands.set(commands);
    const client = {updateSettings: vi.fn().mockRejectedValue(new Error('Failed'))} as unknown as BridgeClient;
    await expect(commitSettingsActions(client, ['developer.enable'])).rejects.toThrow('Failed');
    expect($slashCommands.get()).toEqual(commands);
  });
  it('fits both permission actions with four status rows and a fixed footer at 24 rows', () => {
    expect(settingsActionWindowSize(24, 2, 4, 2)).toBe(2);
    expect(settingsActionWindowSize(24, 5, null, 0)).toBe(5);
    expect(settingsActionWindowSize(40, 2, 4, 2)).toBe(2);
    expect(settingsActionWindowSize(16, 8, 4, 2)).toBe(1);
  });
  it('reads grouped settings and formats read-only values', () => {
    const categories = readSettingsCategories({categories: [{id: 'permissions', label: 'Permissions', description: 'desc', actions: [{id: 'allow-edits', label: 'Edit files permission', kind: 'info', value: false}]}]});
    expect(categories[0].label).toBe('Permissions');
    expect(formatActionValue(categories[0].actions[0])).toBe('current: false');
    expect(formatActionValue({id: 'budget', label: 'Set budget', kind: 'info', value: 'Use /memory budget set <stm|wm|both> <tokens>'})).toContain('Use /memory budget set');
    expect(formatActionValue({id: 'mode', label: 'Execution mode', kind: 'choice', value: 'project', options: [{value: 'project', label: 'Unattended for this project', action: 'unattended.project'}]}, 'unattended.project')).toBe('Unattended for this project');
    expect(formatActionValue({id: 'reset', label: 'Reset temporary grants', kind: 'action', action: 'permissions.reset'})).toBe('');
    expect(formatCurrentActionValue({id: 'mode', label: 'Execution mode', kind: 'choice', value: 'project', options: [{value: 'project', label: 'Unattended for this project', action: 'unattended.project'}]})).toBe('Unattended for this project');
  });

  it('keeps section headings and status rows out of keyboard selection', () => {
    const actions = [
      {id: 'heading', label: 'Permission state', kind: 'section' as const},
      {id: 'status', label: 'Commands', kind: 'info' as const, value: 'Ask each time'},
      {id: 'reset', label: 'Reset temporary grants', kind: 'action' as const, action: 'permissions.reset'},
      {id: 'mode', label: 'Execution mode', kind: 'choice' as const, value: 'interactive', options: []}
    ];
    expect(isSelectableAction(actions[0])).toBe(false);
    expect(isSelectableAction(actions[1])).toBe(false);
    expect(isSelectableAction({id: 'status', label: 'Status', kind: 'status'})).toBe(false);
    expect(firstSelectableIndex(actions)).toBe(2);
    expect(nextSelectableIndex(actions, 2, 1)).toBe(3);
    expect(nextSelectableIndex(actions, 3, -1)).toBe(2);
  });
});
