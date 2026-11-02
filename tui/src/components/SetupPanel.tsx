import React, {useEffect, useState} from 'react';
import {Box, Text, useInput} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {$activeView, $config, $configReturnView, $setup, $setupPage, $terminalColumns, $terminalRows} from '../stores/runtime.js';
import {notifySettingsResult} from '../notifications.js';
import type {BridgeClient} from '../protocol/client.js';
import {TextInput} from './TextInput.js';

const names: Record<string, string> = {llm: 'LLM', embedding: 'Embedding - BGE-M3', reranker: 'Reranker - Qwen3 0.6B', search: 'Search - Tavily', remote: 'Remote - Telegram'};

export function SetupPanel({client}: {client: BridgeClient}) {
  const snapshot = useStore($setup);
  const page = useStore($setupPage);
  const columns = useStore($terminalColumns);
  const rows = useStore($terminalRows);
  const [index, setIndex] = useState(0);
  const [draft, setDraft] = useState<Record<string, string | boolean>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [restartConfirm, setRestartConfirm] = useState(false);
  const component = snapshot?.components?.[page];
  const fields: Array<{key: string; label: string; secret: boolean}> = component?.fields || [];
  const listing = page === 'overview' || page === 'model';
  const entries = listing ? (page === 'model' ? ['llm', 'embedding', 'reranker'] : ['llm', 'embedding', 'reranker', 'search', 'remote']) : [];
  const fieldActions = fields.flatMap(field => field.secret ? [field.key, `clear_${field.key}`] : [field.key]);
  const actions = restartConfirm ? ['Cancel', 'Restart Backend'] : listing
    ? [...entries, 'Refresh checks', 'Restart Backend', ...(page === 'overview' ? [...(!snapshot?.ltm_enabled ? ['Continue without LTM'] : []), 'Continue'] : []), 'Back']
    : [...fieldActions, 'Test saved configuration', 'Cancel', 'Confirm'];

  useEffect(() => {setIndex(0); setDraft({}); setError(''); setEditing(null);}, [page]);
  useEffect(() => {void refresh();}, []);

  async function refresh() {
    try {$setup.set(await client.request('setup.status'));} catch (err) {setError(String(err));}
  }
  function back() {
    setDraft({});
    if (listing || page === 'search' || page === 'remote') $activeView.set('settings');
    else $setupPage.set('model');
  }
  async function perform(action: string) {
    if (busy) return;
    if (restartConfirm && action === 'Cancel') {setRestartConfirm(false); setIndex(0); return;}
    if (action === 'Restart Backend' && !restartConfirm) {setRestartConfirm(true); setIndex(0); return;}
    if (action === 'Back' || action === 'Cancel') {
      if (action === 'Cancel') notifySettingsResult('Changes were not saved.');
      back(); return;
    }
    if (entries.includes(action)) {$setupPage.set(action); return;}
    if (fieldActions.includes(action)) {
      if (action.startsWith('clear_')) {setDraft({...draft, [action]: !draft[action]}); return;}
      setEditing(action); setText(fields.find(f => f.key === action)?.secret ? '' : String(draft[action] ?? component.values[action] ?? '')); return;
    }
    setBusy(true); setError('');
    try {
      if (action === 'Refresh checks') await refresh();
      else if (action === 'Test saved configuration') {$setup.set(await client.request('setup.test', {component: page}));}
      else if (action === 'Confirm') {
        $setup.set(await client.request('settings.update', {component: page, updates: draft}));
        notifySettingsResult(Object.keys(draft).length ? 'Settings saved. Check Status for restart requirements.' : 'No changes to save.'); back();
      } else if (action === 'Continue without LTM') {
        $setup.set(await client.request('setup.continueWithoutLtm')); $activeView.set('chat');
      } else if (action === 'Continue') {
        const result: any = await client.request('setup.status'); $setup.set(result);
        if (!result.ltm_enabled || result.components.llm.status !== 'Ready') throw new Error('Complete required tests, or choose Continue without LTM.');
        $activeView.set('chat');
      } else if (action === 'Restart Backend') {
        const result = await client.restartBackend(); $setup.set((result as any).setup); setRestartConfirm(false); setIndex(0);
      }
    } catch (err) {setError(String(err));} finally {setBusy(false);}
  }
  useEffect(() => {
    if (page !== 'llm') return;
    setBusy(true);
    client.getConfig().then(result => {$config.set(result as Record<string, unknown>); $configReturnView.set('setup'); $setupPage.set('model'); $activeView.set('config');}).catch(err => setError(String(err))).finally(() => setBusy(false));
  }, [page]);
  useInput((input, key) => {
    if (busy) return;
    if (editing) {if (key.escape) setEditing(null); return;}
    if (key.escape) {if (restartConfirm) {setRestartConfirm(false);setIndex(0);} else back();return;}
    if (key.upArrow) setIndex(i => Math.max(0, i - 1));
    if (key.downArrow) setIndex(i => Math.min(actions.length - 1, i + 1));
    if (key.return) void perform(actions[index]);
  });
  const footerCount = !listing && !restartConfirm ? 2 : 1;
  const body = actions.slice(0, -footerCount);
  const footer = actions.slice(-footerCount);
  const size = Math.max(1, rows - (listing ? entries.length + (page === 'overview' ? Object.keys(snapshot?.infrastructure || {}).length + 12 : 10) : fields.length + 15));
  const start = Math.max(0, Math.min(index, Math.max(0, body.length - 1)) - size + 1);
  return <Box flexDirection="column" width={Math.max(20, columns - 4)}>
    <Text bold>Sema settings / {names[page] || (page === 'model' ? 'Model' : 'Preparation')}</Text>
    <Text bold>Status</Text>
    {restartConfirm ? <Text>Restart disconnects clients for this project. Running work must finish first.</Text> : listing ? <>
      {entries.map(name => <Text key={name}>{names[name]}: {snapshot?.components?.[name]?.status || 'Checking'}</Text>)}
      {page === 'overview' ? <Text wrap="truncate-end">Without LTM: STM/WM remain active; expired LTM input is permanently skipped.</Text> : null}
      {page === 'overview' ? Object.entries(snapshot?.infrastructure || {}).map(([name, state]) => <Text key={name}>{name}: {String(state)}</Text>) : null}
    </> : <>
      <Text>{component?.status || 'Checking'}{component?.detail ? `: ${component.detail}` : ''}</Text>
      {component?.model ? <Text>Model: {component.model}{component.dimension ? ` (${component.dimension} dimensions)` : ''}</Text> : null}
      {component?.device ? <Text>Last tested device: {component.device}</Text> : null}
      {fields.map(field => <Text key={field.key} wrap="truncate-end">{field.label}: {field.secret ? component?.secrets?.[field.key] ? 'Configured' : 'Missing' : component?.values?.[field.key] || 'Missing'} ({component?.sources?.[field.key]})</Text>)}
      {page === 'remote' ? <Text>Restart Gateway after saving. Check verifies bot/chat access, not user identity.</Text> : null}
      {Object.values(component?.sources || {}).some(source => String(source).startsWith('environment')) ? <Text>Environment overrides saved values. Change the environment to use saved configuration.</Text> : null}
    </>}
    {error ? <Text color="red" wrap="wrap">{error}</Text> : null}
    <Text bold>Actions</Text>
    {body.slice(start, start + size).map((action, offset) => <Text key={action} inverse={index === start + offset} wrap="truncate-end">{names[action] || fields.find(f => f.key === action)?.label || action}{action.startsWith('clear_') && draft[action] ? ' [selected]' : fields.some(f => f.key === action) && action in draft ? fields.find(f => f.key === action)?.secret ? ': Replacement entered' : `: ${draft[action]}` : ''}</Text>)}
    <Text>------------------------</Text>
    {footer.map((action, offset) => <Text key={action} inverse={index === body.length + offset}>{action}</Text>)}
    {busy ? <Text>Checking / applying...</Text> : null}
    {editing ? <TextInput secret={fields.find(f => f.key === editing)?.secret} value={text} onChange={setText} onSubmit={value => {
      const secret = fields.find(f => f.key === editing)?.secret;
      if (value || !secret) setDraft({...draft, [editing]: value, ...(secret ? {[`clear_${editing}`]: false} : {})});
      setEditing(null);
    }} /> : null}
  </Box>;
}
