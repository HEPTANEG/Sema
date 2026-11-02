import React, {useEffect, useState} from 'react';
import {Box, Text, useInput} from '@hermes/ink';
import type {BridgeClient} from '../protocol/client.js';
import {TextInput} from './TextInput.js';
import {theme} from '../theme.js';

type Status = {db_path: string; live_object_count: number; deleted_object_count: number;
  database_bytes: number; vector_bytes: number; warning_mib: number | null; over_warning: boolean};
type Preview = {preview_token: string; object_count: number; db_path: string; warning: string};
type Purge = {status: string; message?: string; removed_count?: number; compaction_error?: string;
  before?: {total_bytes: number}; after?: {total_bytes: number}};
const mib = (bytes: number) => (bytes / 1048576).toFixed(2);

export function LtmStoragePanel({client, onBack}: {client: BridgeClient; onBack: () => void}) {
  const [status, setStatus] = useState<Status | null>(null);
  const [selection, setSelection] = useState(0);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [retryToken, setRetryToken] = useState<string | null>(null);
  const refresh = async () => setStatus(await client.request('ltm.storage.status', {}) as Status);
  useEffect(() => {void refresh().catch(error => setNotice(String(error)));}, [client]);
  async function run(action: () => Promise<void>) {
    setBusy(true);
    try {await action(); await refresh();} catch (error) {setNotice(String(error));}
    finally {setBusy(false);}
  }
  async function purge(token: string) {
    const result = await client.request('ltm.storage.purge', {preview_token: token, confirmed: true}) as Purge;
    setPreview(null);
    if (result.status === 'busy') {setNotice(result.message || 'Memory tasks are active; retry later.'); return;}
    setRetryToken(result.status === 'compaction_failed' ? token : null);
    setNotice(`Records removed: ${result.removed_count || 0}. ${result.before && result.after ? `${mib(result.before.total_bytes)} → ${mib(result.after.total_bytes)} MiB.` : ''}${result.compaction_error ? ` File compaction incomplete: ${result.compaction_error}` : ''}`);
  }
  const labels = ['Set capacity warning (MiB; blank disables)', 'Permanently purge logically deleted memories', ...(retryToken ? ['Retry file compaction'] : []), 'Back'];
  useInput((_input, key) => {
    if (busy) return;
    if (key.escape) {if (editing) setEditing(false); else if (preview) setPreview(null); else onBack(); return;}
    if (editing) return;
    if (preview) {
      if (key.leftArrow || key.rightArrow) setSelection(selection === 0 ? 1 : 0);
      if (key.return) {if (selection === 1) void run(() => purge(preview.preview_token)); else setPreview(null);}
      return;
    }
    if (key.upArrow) setSelection(Math.max(0, selection - 1));
    if (key.downArrow) setSelection(Math.min(labels.length - 1, selection + 1));
    if (key.return) {
      if (selection === 0) {setValue(status?.warning_mib == null ? '' : String(status.warning_mib)); setEditing(true);}
      else if (selection === 1) void run(async () => {setPreview(await client.request('ltm.storage.preview', {}) as Preview); setSelection(0);});
      else if (retryToken && selection === 2) void run(() => purge(retryToken));
      else onBack();
    }
  });
  return <Box flexDirection="column">
    <Text bold color={theme.config}>Settings / LTM Storage</Text>
    {status && <>
      <Text>{status.db_path}</Text>
      <Text>Live: {status.live_object_count} | Logically deleted: {status.deleted_object_count}</Text>
      <Text>Database: {mib(status.database_bytes)} MiB | Vector index: {mib(status.vector_bytes)} MiB</Text>
      <Text color={status.over_warning ? theme.warning : undefined}>Capacity warning: {status.warning_mib ?? 'disabled'}{status.over_warning ? ' — exceeded (writes remain enabled)' : ''}</Text>
    </>}
    <Text dimColor>Excludes Event Stream, diagnostics, models and other caches. This is not privacy erasure.</Text>
    {busy ? <Text>Working…</Text> : editing ? <TextInput value={value} onChange={setValue} onSubmit={text => {
      const n = text.trim() ? Number(text) : null;
      if (n !== null && (!Number.isFinite(n) || n <= 0)) {setNotice('Enter a positive MiB value, or leave blank.'); return;}
      setEditing(false);
      void run(async () => {await client.request('ltm.storage.warning', {warning_mib: n}); setNotice('Capacity warning updated.');});
    }} /> : preview ? <>
      <Text color={theme.warning}>Permanently remove {preview.object_count} memories from {preview.db_path}?</Text>
      <Text>{preview.warning}</Text>
      <Text>{selection === 0 ? '> ' : '  '}Cancel   {selection === 1 ? '> ' : '  '}Confirm irreversible cleanup</Text>
    </> : labels.map((label, index) => <Text key={label} color={selection === index ? theme.config : undefined}>{selection === index ? '> ' : '  '}{label}</Text>)}
    {notice && <Text>{notice}</Text>}
    <Text dimColor>Arrows select; Enter confirms; Esc returns.</Text>
  </Box>;
}
