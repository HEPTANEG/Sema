import React, {useEffect, useMemo, useRef, useState} from 'react';
import {Box, Text, useInput} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {$activeView, $config, $settings, $terminalColumns, $terminalRows, applySettingsSnapshot, closeActiveView, failRequest, finishActivity, setBusy} from '../stores/runtime.js';
import {theme} from '../theme.js';
import {GutterBlock} from './GutterBlock.js';
import type {BridgeClient} from '../protocol/client.js';
import {configOverlayHeight} from './ConfigPanel.js';
import {notifySettingsResult} from '../notifications.js';
import {$setup, $setupPage} from '../stores/runtime.js';
import {LtmStoragePanel} from './LtmStoragePanel.js';

export type SettingsOption = {
  value: string;
  label: string;
  action: string;
  description?: string;
  warning?: string;
  confirm_label?: string;
};

export type SettingsAction = {
  id: string;
  label: string;
  kind?: 'config' | 'info' | 'status' | 'section' | 'choice' | 'toggle' | 'action' | 'category' | 'back' | 'cancel' | 'confirm' | 'close' | 'submenu';
  command?: string;
  action?: string;
  action_on?: string;
  action_off?: string;
  value?: unknown;
  warning?: string;
  confirm_label?: string;
  description?: string;
  options?: SettingsOption[];
};

export type SettingsCategory = {
  id: string;
  label: string;
  description: string;
  actions: SettingsAction[];
};

type ConfirmationPrompt = {action: string; warning: string; label: string};

export async function commitSettingsActions(client: BridgeClient, actions: string[]) {
  for (const action of actions) {
    const snapshot = await client.updateSettings(action) as Record<string, unknown>;
    applySettingsSnapshot(snapshot);
  }
}

export function SettingsPanel({client}: {client: BridgeClient}) {
  const activeView = useStore($activeView);
  const settings = useStore($settings);
  const terminalColumns = useStore($terminalColumns);
  const terminalRows = useStore($terminalRows);
  const [categoryId, setCategoryId] = useState<string | null>(null);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [draftActions, setDraftActions] = useState<Record<string, string>>({});
  const [confirmation, setConfirmation] = useState<ConfirmationPrompt | null>(null);
  const selectedIndexRef = useRef(0);
  const wasOpenRef = useRef(false);
  const categories = useMemo(() => readSettingsCategories(settings), [settings]);
  const category = categories.find(item => item.id === categoryId) || null;
  const editable = Boolean(category?.actions.some(action => action.kind === 'choice' || action.kind === 'toggle'));
  const statusItems = category?.actions.filter(action => action.kind === 'info' || action.kind === 'status' || action.kind === 'choice' || action.kind === 'toggle') || [];
  const actionItems = category?.actions.filter(action => action.kind === 'config' || action.kind === 'action' || action.kind === 'choice' || action.kind === 'toggle') || [];
  const actionList = actionItems;
  const footerActions: SettingsAction[] = category
    ? editable
      ? [{id: 'cancel', label: 'Cancel', kind: 'cancel' as const}, {id: 'confirm', label: 'Confirm', kind: 'confirm' as const}]
      : [{id: 'back', label: 'Back', kind: 'back' as const}]
    : [];
  const actions: SettingsAction[] = category
    ? [...actionList, ...footerActions]
    : [...categories.map(item => ({id: item.id, label: item.label, kind: 'category' as const, description: item.description})), {id: 'done', label: 'Done', kind: 'close' as const, description: 'close settings'}];
  const bodySelectionIndex = selectedIndex < actionList.length
    ? selectedIndex
    : Math.max(0, actionList.length - 1);
  const listActions = category ? actionList : actions;
  const listSelection = category ? bodySelectionIndex : selectedIndex;
  const statusRowCount = statusItems.reduce((count, item) => count + (item.kind === 'status' ? formatStatusRows(item.value).length : 1), 0);
  const visibleSize = settingsActionWindowSize(terminalRows, listActions.length, category ? statusRowCount : null, footerActions.length);
  const start = Math.min(Math.max(0, listSelection - visibleSize + 1), Math.max(0, listActions.length - visibleSize));

  useEffect(() => {
    const open = Boolean(settings);
    if (open && !wasOpenRef.current) {
      setCategoryId(null);
      setConfirmation(null);
      setDraftActions({});
      selectedIndexRef.current = 0;
      setSelectedIndex(0);
    }
    wasOpenRef.current = open;
  }, [settings]);

  useEffect(() => {
    if (!category) {
      setDraftActions({});
      return;
    }
    const next: Record<string, string> = {};
    for (const action of category.actions) {
      if (action.kind === 'toggle') {
        next[action.id] = Boolean(action.value) ? String(action.action_on || '') : String(action.action_off || '');
      } else if (action.kind === 'choice') {
        const selected = action.options?.find(option => option.value === String(action.value));
        if (selected) next[action.id] = selected.action;
      }
    }
    setDraftActions(next);
  }, [categoryId, settings]);

  useEffect(() => {
    const bounded = Math.max(0, Math.min(actions.length - 1, selectedIndexRef.current));
    selectedIndexRef.current = isSelectableAction(actions[bounded]) ? bounded : firstSelectableIndex(actions);
    setSelectedIndex(selectedIndexRef.current);
  }, [actions.length, categoryId]);

  useInput((_input, key) => {
    if (!settings || activeView !== 'settings') return;
    if (confirmation) {
      if (key.escape || key.leftArrow || key.upArrow) {
        setConfirmation(null);
      } else if (key.rightArrow || key.downArrow) {
        setSelectedIndex(1);
      } else if (key.return) {
        if (selectedIndex === 0) setConfirmation(null);
        else void commitAction(confirmation.action);
      }
      return;
    }
    if (key.escape || _input === 'q' || _input === 'x') {
      if (categoryId) {
        setCategoryId(null);
        notifySettingsResult('Changes were not saved.');
      }
      else closeActiveView();
      resetSelection();
    } else if (key.upArrow) {
      moveSelection(-1);
    } else if (key.downArrow) {
      moveSelection(1);
    } else if (key.leftArrow || key.rightArrow) {
      adjustSelected(key.leftArrow ? -1 : 1);
    } else if (key.return) {
      void executeAction(actions[selectedIndex]);
    }
  }, {isActive: activeView === 'settings' && Boolean(settings) && categoryId !== 'ltm-storage'});

  if (!settings) return null;

  async function executeAction(action: SettingsAction | undefined) {
    if (!action) return;
    if (action.kind === 'close') return closeActiveView();
    if (action.kind === 'back' || action.kind === 'cancel') {
      if (categoryId) {
        setCategoryId(null);
        if (action.kind === 'cancel') notifySettingsResult('Changes were not saved.');
      }
      else closeActiveView();
      setDraftActions({});
      resetSelection();
      return;
    }
    if (action.kind === 'confirm') {
      await confirmDraft();
      return;
    }
    if (!categoryId && ['model', 'search', 'remote', 'overview'].includes(action.id)) {
      try {
        $setup.set(await client.request('setup.status'));
        $setupPage.set(action.id);
        $activeView.set('setup');
      } catch (error) {failRequest(error);}
      return;
    }
    if (!categoryId && action.id !== 'model') {
      setCategoryId(action.id);
      resetSelection();
      return;
    }
    if (action.kind === 'config' || (!categoryId && action.id === 'model')) {
      setBusy('loading config', true);
      try {
        $config.set((await client.getConfig()) as Record<string, unknown>);
        $activeView.set('config');
        finishActivity('ready');
      } catch (error) {
        failRequest(error);
      }
      return;
    }
    if (action.kind === 'action' && action.action) {
      selectedIndexRef.current = 0;
      setSelectedIndex(0);
      setConfirmation({action: action.action, warning: action.warning || 'Confirm this Settings action.', label: action.confirm_label || action.label});
    }
  }

  function adjustSelected(delta: number) {
    const action = actions[selectedIndex];
    if (!action || (action.kind !== 'choice' && action.kind !== 'toggle')) return;
    if (action.kind === 'toggle') {
      const current = draftActions[action.id] || action.action_off || '';
      const next = current === action.action_on ? action.action_off : action.action_on;
      setDraftActions(values => ({...values, [action.id]: String(next || '')}));
      return;
    }
    const options = action.options || [];
    if (!options.length) return;
    const currentIndex = Math.max(0, options.findIndex(option => option.action === draftActions[action.id]));
    const nextIndex = (currentIndex + delta + options.length) % options.length;
    setDraftActions(values => ({...values, [action.id]: options[nextIndex].action}));
  }

  async function confirmDraft() {
    if (!category) return;
    const pending = Object.values(draftActions).filter(Boolean);
    const dangerous = category.actions.find(action => {
      if (action.kind === 'choice') {
        const option = action.options?.find(item => item.action === draftActions[action.id]);
        return Boolean(option?.warning);
      }
      return false;
    });
    if (dangerous && dangerous.kind === 'choice') {
      const option = dangerous.options?.find(item => item.action === draftActions[dangerous.id]);
      if (option?.warning) {
        selectedIndexRef.current = 0;
        setSelectedIndex(0);
        setConfirmation({action: option.action, warning: option.warning, label: option.confirm_label || option.label});
        return;
      }
    }
    if (!pending.length) return;
    await commitActions(pending);
  }

  async function commitAction(action: string) {
    setConfirmation(null);
    const independentAction = category?.actions.some(item => item.kind === 'action' && item.action === action);
    await commitActions([action], !independentAction);
  }

  async function commitActions(actionsToApply: string[], returnToRoot = true) {
    setBusy('updating settings', true);
    try {
      await commitSettingsActions(client, actionsToApply);
      notifySettingsResult('New settings are now active.');
      if (returnToRoot) setCategoryId(null);
      setDraftActions({});
      resetSelection();
      finishActivity('ready');
    } catch (error) {
      failRequest(error);
    }
  }

  function moveSelection(delta: number) {
    const next = nextSelectableIndex(actions, selectedIndexRef.current, delta);
    selectedIndexRef.current = next;
    setSelectedIndex(next);
  }

  function resetSelection() {
    selectedIndexRef.current = 0;
    setSelectedIndex(0);
  }

  if (categoryId === 'ltm-storage') return <LtmStoragePanel client={client} onBack={() => {setCategoryId(null); resetSelection();}} />;
  const contentWidth = Math.max(24, terminalColumns - 5);
  return (
    <GutterBlock kind="important" markerColor={theme.config}>
      <Box width={contentWidth} flexDirection="column">
        <Text color={theme.config}>Sema settings{category ? ` / ${category.label}` : ''}</Text>
        {category ? <Text dimColor wrap="truncate-end">{category.description}</Text> : <Text dimColor>Choose a category.</Text>}
        {confirmation ? (
          <Box flexDirection="column" marginTop={1}>
            <Text color={theme.warning} bold>Confirm Settings action</Text>
            <Text wrap="wrap">{confirmation.warning}</Text>
            <Text color={selectedIndex === 0 ? theme.config : undefined} bold={selectedIndex === 0}>{selectedIndex === 0 ? '█ ' : '  '}Cancel</Text>
            <Text color={selectedIndex === 1 ? theme.warning : undefined} bold={selectedIndex === 1}>{selectedIndex === 1 ? '█ ' : '  '}{confirmation.label}</Text>
            <Text dimColor>Use Left/Right and Enter. Esc cancels.</Text>
          </Box>
        ) : category ? (
          <>
            {statusItems.length ? (
              <Box flexDirection="column" marginTop={1}>
                <Text color={theme.config} bold>Status</Text>
                {statusItems.flatMap(item => item.kind === 'status'
                  ? formatStatusRows(item.value).map(row => <Text key={`${item.id}-${row.label}`} wrap="truncate-end">{row.label}: {row.value}</Text>)
                  : [<Text key={item.id} dimColor wrap="truncate-end">{item.label}  {formatCurrentActionValue(item)}</Text>]
                )}
              </Box>
            ) : null}
            {actionList.length ? <Box flexDirection="column" marginTop={1}>
              <Text dimColor bold>Actions</Text>
              {actionList.slice(start, start + visibleSize).map((item, offset) => {
                const index = start + offset;
                const selected = index === selectedIndex && isSelectableAction(item);
                const value = formatActionValue(item, draftActions[item.id]);
                return item.kind === 'section'
                  ? <Text key={item.id} dimColor bold>{item.label}</Text>
                  : <Text key={item.id} color={selected ? theme.config : undefined} dimColor={item.kind === 'info'} bold={selected} wrap="truncate-end">{selected ? '█ ' : '  '}{item.label}{value ? `  ${value}` : ''}{item.description ? ` - ${item.description}` : ''}</Text>;
              })}
            </Box> : null}
            {footerActions.length ? (
              <Box flexDirection="column" marginTop={1}>
                <Text dimColor>{'-'.repeat(Math.min(contentWidth, 48))}</Text>
                {footerActions.map(action => {
                  const index = actionList.length + footerActions.indexOf(action);
                  const selected = index === selectedIndex;
                  return <Text key={action.id} color={selected ? theme.config : undefined} bold={selected}>{selected ? '█ ' : '  '}{action.label}</Text>;
                })}
              </Box>
            ) : null}
          </>
        ) : (
          <Box flexDirection="column" marginTop={1}>
            {listActions.slice(start, start + visibleSize).map((item, offset) => {
              const index = start + offset;
              const selected = index === selectedIndex && isSelectableAction(item);
              return <Text key={item.id} color={selected ? theme.config : undefined} bold={selected} wrap="truncate-end">{selected ? '█ ' : '  '}{item.label}{item.description ? ` - ${item.description}` : ''}</Text>;
            })}
          </Box>
        )}
        {!confirmation && <Text dimColor>{category && editable ? 'Use Up/Down, Left/Right, and Enter. Confirm applies changes. Esc cancels.' : category ? 'Use Up/Down and Enter. Esc goes back.' : 'Use Up/Down and Enter. Esc closes.'}</Text>}
      </Box>
    </GutterBlock>
  );
}

export function settingsActionWindowSize(terminalRows: number, actionCount: number, statusRows: number | null, footerRows: number) {

  const reservedRows = statusRows == null ? 4 : 9 + statusRows + footerRows;
  return Math.max(1, Math.min(actionCount || 1, configOverlayHeight(terminalRows) - reservedRows));
}

export function readSettingsCategories(settings: Record<string, unknown> | null): SettingsCategory[] {
  const categories = settings?.categories;
  if (!Array.isArray(categories)) return [];
  return categories.filter(item => item && typeof item === 'object').map(item => {
    const record = item as Record<string, unknown>;
    const actions = Array.isArray(record.actions) ? record.actions : [];
    return {id: String(record.id || ''), label: String(record.label || record.id || ''), description: String(record.description || ''), actions: actions.filter(action => action && typeof action === 'object').map(action => action as SettingsAction)};
  }).filter(item => item.id);
}

export function formatActionValue(action: SettingsAction, draftAction?: string) {
  if (action.kind === 'toggle') return draftAction === action.action_on ? 'On' : 'Off';
  if (action.kind === 'choice') return action.options?.find(option => option.action === draftAction)?.label || String(action.value || '');
  if (action.kind === 'info') return typeof action.value === 'string' ? action.value : `current: ${String(Boolean(action.value))}`;
  if (action.kind === 'submenu') return typeof action.value === 'string' ? action.value : '';
  if (action.kind === 'status') return 'View status';
  return '';
}

export function formatCurrentActionValue(action: SettingsAction) {
  if (action.kind === 'toggle') return Boolean(action.value) ? 'On' : 'Off';
  if (action.kind === 'choice') return action.options?.find(option => option.value === String(action.value))?.label || String(action.value || '');
  return formatActionValue(action);
}

export function formatStatusRows(value: unknown) {
  if (!value || typeof value !== 'object') return [{label: 'Status', value: String(value || '')}];
  return Object.entries(value as Record<string, unknown>).filter(([label]) => label !== 'api_key').map(([label, item]) => ({label: label.replace(/_/g, ' '), value: typeof item === 'object' ? JSON.stringify(item) : String(item)}));
}

export function isSelectableAction(action: SettingsAction | undefined) {
  return Boolean(action && action.kind !== 'info' && action.kind !== 'status' && action.kind !== 'section');
}

export function firstSelectableIndex(actions: SettingsAction[]) {
  const index = actions.findIndex(isSelectableAction);
  return index < 0 ? 0 : index;
}

export function nextSelectableIndex(actions: SettingsAction[], current: number, delta: number) {
  const direction = delta < 0 ? -1 : 1;
  let index = current + direction;
  while (index >= 0 && index < actions.length) {
    if (isSelectableAction(actions[index])) return index;
    index += direction;
  }
  return current;
}
