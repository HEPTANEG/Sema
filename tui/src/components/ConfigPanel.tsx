import React, {useEffect, useMemo, useRef, useState} from 'react';
import {Box, Text, useInput} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {$activeView, $config, $terminalColumns, $terminalRows, applyConfigUpdates, returnToSettings} from '../stores/runtime.js';
import {theme} from '../theme.js';
import {stripTerminalControlInput} from '../inputSanitizer.js';
import {GutterBlock} from './GutterBlock.js';
import type {BridgeClient} from '../protocol/client.js';
import {TextInput} from './TextInput.js';
import {$setup} from '../stores/runtime.js';

type ConfigKey = 'provider' | 'model' | 'base_url' | 'api_key_env' | 'api_key';
type ChoiceKey = 'protocol';
type ToggleKey = 'thinking';
type ConfigSection = 'provider' | 'connection' | 'capabilities';

type ConfigAction =
  | {id: string; kind: 'test' | 'clear'; label: string; description: string; section: ConfigSection}
  | {id: string; kind: 'edit'; label: string; description: string; key: ConfigKey; section: ConfigSection}
  | {id: string; kind: 'choice'; label: string; description: string; key: ChoiceKey; options: string[]; section: ConfigSection}
  | {id: string; kind: 'toggle'; label: string; description: string; key: ToggleKey; section: ConfigSection}
  | {id: string; kind: 'cancel' | 'confirm'; label: string; description: string};
type ContentConfigAction = Exclude<ConfigAction, {kind: 'cancel' | 'confirm'}>;

const editableConfigKeys = ['provider', 'protocol', 'base_url', 'model', 'api_key_env', 'thinking', 'api_key', 'clear_api_key'];
const protocolOptions = ['openai_compatible', 'ollama_native'];

export function ConfigPanel({client}: {client: BridgeClient}) {
  const activeView = useStore($activeView);
  const config = useStore($config);
  const setup = useStore($setup);
  const terminalColumns = useStore($terminalColumns);
  const terminalRows = useStore($terminalRows);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const selectedIndexRef = useRef(0);
  const [editing, setEditing] = useState<ConfigKey | null>(null);
  const [draftValue, setDraftValue] = useState('');
  const [configError, setConfigError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);

  const current = ((config?.config as Record<string, unknown>) || {}) as Record<string, unknown>;
  const profiles = ((config?.profiles as Record<string, unknown>) || {}) as Record<string, unknown>;
  const [draftConfig, setDraftConfig] = useState<Record<string, unknown>>(current);
  const actions = useMemo(() => buildConfigActions(profiles, draftConfig), [profiles, draftConfig]);
  const contentActions = actions.filter((action): action is ContentConfigAction => action.kind !== 'cancel' && action.kind !== 'confirm');
  const footerActions = actions.filter(action => action.kind === 'cancel' || action.kind === 'confirm');
  const overlayHeight = configOverlayHeight(terminalRows);
  const actionWindowSize = Math.max(1, Math.min(contentActions.length, overlayHeight - 11));
  const contentSelectionIndex = selectedIndex < contentActions.length ? selectedIndex : Math.max(0, contentActions.length - 1);
  const actionWindowStart = configActionWindowStart(contentSelectionIndex, contentActions.length, actionWindowSize);
  const visibleActions = contentActions.slice(actionWindowStart, actionWindowStart + actionWindowSize);
  const protocolMismatch = providerProtocolMismatch(draftConfig, profiles);
  const hasUnsavedChanges = editableConfigKeys.some(key => draftConfig[key] !== current[key]);
  const wasOpenRef = useRef(false);

  useEffect(() => {
    if (config) {
      setDraftConfig(current);
      setConfigError(null);
    }
  }, [config]);

  useEffect(() => {
    const next = Math.max(0, Math.min(actions.length - 1, selectedIndexRef.current));
    selectedIndexRef.current = next;
    setSelectedIndex(next);
  }, [actions.length]);

  useEffect(() => {
    const isOpen = Boolean(config);
    if (isOpen && !wasOpenRef.current) {
      selectedIndexRef.current = 0;
      setSelectedIndex(0);
    }
    wasOpenRef.current = isOpen;
  }, [config]);

  useInput((input, key, event) => {
    if (testing) return;
    if (!config) {
      return;
    }
    if (editing) {
      if (key.escape) {
        setEditing(null);
        setDraftValue('');
      }
      return;
    }
    if (key.escape || input === 'x' || input === 'q') {
      returnToSettings('Changes were not saved.');
      return;
    }
    if (key.upArrow) {
      moveSelection(-1);
      return;
    }
    if (key.downArrow) {
      moveSelection(1);
      return;
    }
    if (key.leftArrow || key.rightArrow) {
      adjustSelection(key.leftArrow ? -1 : 1);
      return;
    }
    if (key.return) {
      void runAction(actions[selectedIndex]);
    }
  }, {isActive: activeView === 'config' && Boolean(config)});

  if (!config) {
    return null;
  }

  async function runAction(action: ConfigAction | undefined) {
    if (!action) {
      return;
    }
    if (action.kind === 'clear') {
      setDraftConfig(values => ({...values, api_key: '', clear_api_key: true}));
      return;
    }
    if (action.kind === 'test') {
      setTesting(true); setConfigError(null);
      try {
        const result: any = await client.request('setup.test', {component: 'llm'});
        $setup.set(result);
        if (result.components.llm.status === 'Failed') setConfigError(result.components.llm.detail);
      } catch (error) {setConfigError(String(error));}
      finally {setTesting(false);}
      return;
    }
    if (action.kind === 'choice' || action.kind === 'toggle') {
      setConfigError(null);
      return;
    }
    if (action.kind === 'cancel') {
      returnToSettings('Changes were not saved.');
      return;
    }
    if (action.kind === 'confirm') {
      const validationError = validateDraftConfig(draftConfig, profiles);
      if (validationError) {
        setConfigError(validationError);
        return;
      }
      const keys = editableConfigKeys;
      const updates = Object.fromEntries(keys.filter(key => draftConfig[key] !== current[key]).map(key => [key, draftConfig[key]]));
      if (Object.keys(updates).length) {
        const saved = await applyConfigUpdates(client, updates);
        if (!saved) return;
      }
      returnToSettings(Object.keys(updates).length ? 'New settings are now active.' : 'No changes to save.');
      return;
    }
    if (action.kind !== 'edit') {
      return;
    }
    setEditing(action.key);
    setDraftValue(String(draftConfig[action.key] ?? ''));
  }

  function adjustSelection(delta: number) {
    const action = actions[selectedIndex];
    if (!action || (action.kind !== 'choice' && action.kind !== 'toggle')) {
      return;
    }
    if (action.kind === 'toggle') {
      setDraftConfig(values => ({...values, [action.key]: !Boolean(values[action.key])}));
      setConfigError(null);
      return;
    }
    const currentIndex = Math.max(0, action.options.indexOf(String(draftConfig[action.key] || action.options[0])));
    const nextIndex = (currentIndex + delta + action.options.length) % action.options.length;
    setDraftConfig(values => ({...values, [action.key]: action.options[nextIndex]}));
    setConfigError(null);
  }

  function moveSelection(delta: number) {
    const next = Math.max(0, Math.min(actions.length - 1, selectedIndexRef.current + delta));
    selectedIndexRef.current = next;
    setSelectedIndex(next);
  }

  async function saveField(value: string) {
    if (!editing) {
      return;
    }
    const nextValue = stripTerminalControlInput(value).trim();
    if (editing === 'api_key') {
      if (nextValue) setDraftConfig(values => ({...values, api_key: nextValue, clear_api_key: false}));
    } else setDraftConfig(values => ({...values, [editing]: nextValue}));
    setConfigError(null);
    setEditing(null);
    setDraftValue('');
  }
  const contentWidth = Math.max(24, terminalColumns - 5);
  return (
    <GutterBlock kind="important" markerColor={theme.config}>
      <Box width={contentWidth} flexDirection="column">
        <Text color={theme.config}>Model config</Text>
        <Text dimColor>Status{setup?.components?.llm?.status ? ` - LLM: ${testing ? 'Checking' : setup.components.llm.status}` : ''}</Text>
        <Box flexDirection="column">
            {formatCurrentConfig(current).map(row => (
            <Text key={row.label} wrap="truncate-end">{row.label}: {row.value}</Text>
          ))}
        </Box>
        {hasUnsavedChanges ? <Text color={theme.warning}>Unsaved changes - Confirm to save or Cancel to discard.</Text> : null}
        {protocolMismatch ? <Text color={theme.warning} wrap="truncate-end">Configuration warning: {protocolMismatch}</Text> : null}
        {configError ? <Text color={theme.danger} wrap="truncate-end">Cannot save: {configError}</Text> : null}
        {Object.keys((current.environment_overrides || {}) as object).length ? <Text wrap="truncate-end">Environment overrides: {Object.values(current.environment_overrides as object).join(', ')}. Saved values remain overridden.</Text> : null}

        <Box flexDirection="column" marginTop={1}>
          <Text color={theme.config}>Actions</Text>
          {visibleActions.map((action, offset) => {
            const index = actionWindowStart + offset;
            const previousAction = contentActions[index - 1];
            const showSection = action.section !== previousAction?.section;
            return (
              <React.Fragment key={action.id}>
                {showSection ? <Text color={theme.config} bold>{formatConfigSection(action.section)}</Text> : null}
                <Text wrap="truncate-end" color={index === selectedIndex ? theme.config : undefined} bold={index === selectedIndex}>{index === selectedIndex ? '█ ' : '  '}{action.label}{formatConfigActionValue(action, draftConfig)} - {action.description}</Text>
              </React.Fragment>
            );
          })}
          {contentActions.length > actionWindowSize ? (
            <Text dimColor wrap="truncate-end">Showing settings {actionWindowStart + 1}-{Math.min(contentActions.length, actionWindowStart + actionWindowSize)} of {contentActions.length}.</Text>
          ) : null}
        </Box>

        <Box flexDirection="column" marginTop={1}>
          <Text dimColor>{'-'.repeat(Math.min(contentWidth, 48))}</Text>
          {footerActions.map(action => {
            const index = actions.indexOf(action);
            return (
              <Text key={action.id} wrap="truncate-end" color={index === selectedIndex ? theme.config : undefined} bold={index === selectedIndex}>{index === selectedIndex ? '█ ' : '  '}{action.label}</Text>
            );
          })}
        </Box>

        {editing ? (
          <Box marginTop={1}>
            <Text color={theme.warning}>{editing}: </Text>
            <TextInput secret={editing === 'api_key'} columns={Math.max(16, contentWidth - 12)} value={draftValue} onChange={value => setDraftValue(stripTerminalControlInput(value))} onSubmit={saveField} focus />
          </Box>
        ) : (
          <Text dimColor>Use Up/Down to move, Left/Right to change, and Enter to edit. Esc cancels.</Text>
        )}
      </Box>
    </GutterBlock>
  );
}

export function buildConfigActions(profiles: Record<string, unknown>, current: Record<string, unknown>): ConfigAction[] {
  const actions: ConfigAction[] = [];
  actions.push(
    {id: 'edit-provider', kind: 'edit', section: 'provider', key: 'provider', label: 'Provider name', description: 'provider identifier or custom name'},
    {id: 'select-protocol', kind: 'choice', section: 'provider', key: 'protocol', label: 'Protocol', options: protocolOptions, description: 'transport used for model requests'},
    {id: 'edit-base-url', kind: 'edit', section: 'provider', key: 'base_url', label: 'Base URL', description: 'API endpoint for the selected protocol'},
    {id: 'edit-model', kind: 'edit', section: 'provider', key: 'model', label: 'Model', description: 'model name sent to the provider'},
    {id: 'edit-api-key', kind: 'edit', section: 'provider', key: 'api_key', label: 'API key', description: 'leave blank to keep saved key'},
    {id: 'clear-api-key', kind: 'clear', section: 'provider', label: 'Clear saved API key', description: 'applied on Confirm'},
    {id: 'edit-api-key-env', kind: 'edit', section: 'provider', key: 'api_key_env', label: 'API key environment', description: 'environment variable name, not the secret value'},
    {
      id: 'toggle-thinking',
      kind: 'toggle',
      section: 'provider',
      key: 'thinking',
      label: 'Thinking',
      description: 'model reasoning behavior'
    },
    {id: 'test-llm', kind: 'test', section: 'connection', label: 'Test saved configuration', description: 'test the current saved connection'},
    {id: 'cancel', kind: 'cancel', label: 'Cancel', description: 'discard unsaved changes'},
    {id: 'confirm', kind: 'confirm', label: 'Confirm', description: 'save all changes'}
  );
  return actions;
}

export function formatConfigSection(section: ConfigSection) {
  return section === 'provider' ? 'LLM Provider' : section === 'connection' ? 'Connection test' : 'Capabilities';
}

export function expectedProtocolForProvider(provider: string, profiles: Record<string, unknown>) {
  const profile = profiles[provider];
  if (profile && typeof profile === 'object' && typeof (profile as Record<string, unknown>).protocol === 'string') {
    return String((profile as Record<string, unknown>).protocol);
  }
  return null;
}

export function providerProtocolMismatch(current: Record<string, unknown>, profiles: Record<string, unknown>) {
  const provider = String(current.provider || '');
  const actual = String(current.protocol || 'openai_compatible');
  const expected = expectedProtocolForProvider(provider, profiles);
  return !expected || actual === expected ? null : `Provider "${provider}" expects ${expected}, but the draft uses ${actual}. Use a matching protocol or a custom provider name.`;
}

export function formatConfigActionValue(action: ContentConfigAction, current: Record<string, unknown>) {
  if (action.kind === 'choice') {
    const value = String(current[action.key] || action.options[0]);
    return `: ${value === 'ollama_native' ? 'Ollama Native' : 'OpenAI-compatible'}`;
  }
  if (action.kind === 'toggle') {
    return `: ${Boolean(current[action.key]) ? 'On' : 'Off'}`;
  }
  if (action.kind === 'edit') {
    if (action.key === 'api_key') return current.clear_api_key ? ': Will clear' : current.api_key ? ': Replacement entered' : ': Hidden';
    return `: ${String(current[action.key] ?? '')}`;
  }
  return '';
}

export function validateDraftConfig(current: Record<string, unknown>, profiles: Record<string, unknown>) {
  if (!String(current.provider || '').trim()) return 'Provider name is required.';
  const protocol = String(current.protocol || '');
  if (!protocolOptions.includes(protocol)) return 'Select a supported protocol.';
  if (!String(current.base_url || '').trim()) return 'Base URL is required.';
  if (!String(current.model || '').trim()) return 'Model is required.';
  return providerProtocolMismatch(current, profiles);
}

export function configActionWindowStart(selectedIndex: number, actionCount: number, windowSize: number) {
  const maxStart = Math.max(0, actionCount - windowSize);
  const start = Math.max(0, selectedIndex - windowSize + 1);
  return Math.min(start, maxStart);
}

export function formatCurrentConfig(current: Record<string, unknown>) {
  return [
    {label: 'Provider', value: String(current.provider ?? '')},
    {label: 'Protocol', value: current.protocol === 'ollama_native' ? 'Ollama Native' : current.protocol == null || current.protocol === 'openai_compatible' ? 'OpenAI-compatible' : String(current.protocol)},
    {label: 'Base URL', value: String(current.base_url ?? '')},
    {label: 'Model', value: String(current.model ?? '')},
    {label: 'API key environment', value: String(current.api_key_env ?? '(none)')},
    {label: 'API key status', value: formatApiKeyStatus(current)},
    {label: 'Tools', value: capabilityLabel(current.supports_tools)},
    {label: 'Streaming', value: capabilityLabel(current.supports_streaming)},
    {label: 'Vision', value: capabilityLabel(current.supports_vision)},
    {label: 'Thinking', value: current.thinking == null ? 'Provider default' : current.thinking ? 'On' : 'Off'}
  ];
}

function capabilityLabel(value: unknown) {
  return value ? 'Supported' : 'Not supported';
}

export function formatApiKeyStatus(current: Record<string, unknown>) {
  const env = String(current.api_key_env || '(none)');
  const saved = current.api_key_configured || current.api_key ? 'key configured' : 'no saved key';
  return `env ${env}; ${saved}${current.api_key_source ? `; source: ${current.api_key_source}` : ''}`;
}

export function configOverlayHeight(terminalRows: number) {


  return Math.max(12, Math.min(36, Math.max(12, terminalRows) - 5));
}
