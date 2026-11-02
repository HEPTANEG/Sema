import {notificationStatus} from '../notifications.js';
import type {EditStats, ToolRecord} from '../protocol/types.js';

export function formatToolInline(tool: ToolRecord) {
  return `${tool.status} ${formatToolName(tool.tool)}`;
}

export function formatToolName(tool: string) {
  return tool
    .split('_')
    .filter(Boolean)
    .map(part => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    .join(' ');
}

export function formatToolDetail(tool: ToolRecord) {
  return [
    `status: ${tool.status}`,
    `tool: ${formatToolName(tool.tool)}`,
    '',
    'args:',
    formatDetailValue(tool.args),
    '',
    tool.error ? 'error:' : '',
    tool.error || '',
    tool.result ? 'result:' : '',
    tool.result || ''
  ]
    .filter(line => line !== '')
    .join('\n');
}

function formatDetailValue(value: unknown) {
  if (typeof value === 'string') {
    return value;
  }
  return JSON.stringify(value, null, 2) || '';
}

export function formatLastRunStep(step: {tool?: string; args?: unknown; result?: string; error?: string; duration_ms?: number}, index: number) {
  return [
    `${index + 1}. ${step.tool || 'tool'}${step.duration_ms ? ` ${step.duration_ms}ms` : ''}`,
    `args: ${JSON.stringify(step.args)}`,
    step.error ? `error: ${step.error}` : '',
    step.result || ''
  ]
    .filter(Boolean)
    .join('\n');
}

export function formatTraceRecord(record: Record<string, unknown>) {
  const ts = String(record.ts || '');
  const clock = ts.length >= 19 ? ts.slice(11, 19) : ts;
  const event = String(record.event || '?');
  const status = record.status ? ` ${String(record.status)}` : '';
  const tool = record.tool ? ` ${String(record.tool)}` : '';
  const details = formatTraceDetails(record);
  const error = record.error ? ` error=${String(record.error).slice(0, 120)}` : '';
  return `${clock} ${event}${tool}${status}${details}${error}`.trim();
}

function formatTraceDetails(record: Record<string, unknown>) {
  if (record.event === 'reviewer') {
    return formatTraceKeyValues({
      pass: record.passed,
      next: record.recommended_next_action,
      concerns: record.concerns_count,
      source: record.source
    });
  }
  return '';
}

function formatTraceKeyValues(values: Record<string, unknown>) {
  const parts = Object.entries(values)
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => `${key}=${String(value)}`);
  return parts.length ? ` ${parts.join(' ')}` : '';
}

export function formatPermissions(permissions: Record<string, boolean>) {
  const unattended = Boolean(permissions.unattended || permissions.allow_all);
  return [
    `unattended: ${String(unattended)}`,
    `run commands: ${String(Boolean(permissions.allow_run_commands))}`,
    `edit files: ${String(Boolean(permissions.allow_edits))}`,
    '',
    'Use /unattended session or /unattended project to bypass confirmations.',
    'These permissions reset when Sema exits or /permissions reset is used.'
  ].join('\n');
}

export function withLocalNotifyStatus(result: Record<string, unknown>) {
  const status = notificationStatus();
  const content = [
    String(result.content || '').trim(),
    '',
    `focus_tracking_runtime: ${status.focusTrackingStatus}`,
    `terminal_focused: ${String(status.terminalFocused)}`
  ]
    .filter((line, index) => index !== 1 || String(result.content || '').trim())
    .join('\n');
  return {...result, content};
}
