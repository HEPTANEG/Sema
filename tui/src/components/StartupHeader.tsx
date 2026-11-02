import React from 'react';
import {Box, Text} from '@hermes/ink';
import {useStore} from '@nanostores/react';
import {$continuedSession, $model, $permissions, $project, $terminalColumns} from '../stores/runtime.js';

const accentColor = 'cyan';
const fullArt = [
  {color: 'cyan', text: '███████╗ ███████╗ ███╗   ███╗  █████╗ '},
  {color: 'cyanBright', text: '██╔════╝ ██╔════╝ ████╗ ████║ ██╔══██╗'},
  {color: 'blueBright', text: '███████╗ █████╗   ██╔████╔██║ ███████║'},
  {color: 'blue', text: '╚════██║ ██╔══╝   ██║╚██╔╝██║ ██╔══██║'},
  {color: 'magentaBright', text: '███████║ ███████╗ ██║ ╚═╝ ██║ ██║  ██║'},
  {color: 'magenta', text: '╚══════╝ ╚══════╝ ╚═╝     ╚═╝ ╚═╝  ╚═╝'}
] as const;

const compactArt = [
  {color: 'cyan', text: '██████╗ █████╗ ███╗ ███╗ █████╗'},
  {color: 'blueBright', text: '██╔═══╝ ██╔══╝ ████▄████ ██╔══██'},
  {color: 'blue', text: '██████╗ ████╗  ██╔██╔██ ███████'},
  {color: 'magentaBright', text: '╚═══██║ ██╔═╝  ██║╚═╝██ ██╔══██'},
  {color: 'magenta', text: '██████║ █████╗ ██║   ██ ██║  ██'}
] as const;

const tinyArt = [{color: 'cyan', text: '██ SEMA ██'}] as const;

export function StartupHeader({compact = false}: {compact?: boolean}) {
  const columns = useStore($terminalColumns);
  const model = useStore($model);
  const project = useStore($project);
  const permissions = useStore($permissions);
  const continuedSession = useStore($continuedSession);
  const cwd = String(project.root || process.env.SEMA_TUI_WORKSPACE_CWD || process.cwd());
  const session = continuedSession === null ? 'loading' : continuedSession ? 'continued' : 'new';
  const conversation = String(project.active_conversation_id || 'loading');
  const permissionSummary = formatStartupPermissions(permissions);
  const art = compact ? tinyArt : columns >= 48 ? fullArt : columns >= 36 ? compactArt : tinyArt;
  const panelWidth = Math.max(24, columns - 2);
  const valueWidth = Math.max(8, panelWidth - 4);

  if (!compact) {
    return (
      <Box flexDirection="column" width={panelWidth}>
        <Box flexDirection="column" width={panelWidth} marginBottom={1}>
          {art.map(line => (
            <Text key={line.text} color={line.color} bold wrap="truncate-end">
              {fitText(line.text, panelWidth)}
            </Text>
          ))}
        </Box>
        <Box flexDirection="column" borderStyle="single" borderColor={accentColor} paddingX={1} paddingY={1} width={panelWidth}>
          <InfoRow label="Agent" value="Sema" valueWidth={valueWidth} />
          <InfoRow label="model" value={model || 'loading'} valueWidth={valueWidth} />
          <InfoRow label="cwd" value={cwd} valueWidth={valueWidth} />
          <InfoRow label="session" value={session} valueWidth={valueWidth} />
          <InfoRow label="conversation" value={conversation} valueWidth={valueWidth} />
          <InfoRow label="permissions" value={permissionSummary} valueWidth={valueWidth} />
          <InfoRow label="get commands" value="/help" valueWidth={valueWidth} />
        </Box>
      </Box>
    );
  }

  return (
    <Box flexDirection="column" width={panelWidth}>
      <Box flexDirection="column" width={panelWidth}>
        {art.map(line => (
          <Text key={line.text} color={line.color} bold wrap="truncate-end">
            {fitText(line.text, panelWidth)}
          </Text>
        ))}
      </Box>
      <Box flexDirection="column" borderStyle="single" borderColor={accentColor} paddingX={1} width={panelWidth}>
        <InfoRow label="Agent" value="Sema" valueWidth={valueWidth} />
        <InfoRow label="model" value={model || 'loading'} valueWidth={valueWidth} />
        <InfoRow label="cwd" value={cwd} valueWidth={valueWidth} />
        <InfoRow label="session" value={session} valueWidth={valueWidth} />
        <InfoRow label="conversation" value={conversation} valueWidth={valueWidth} />
        <InfoRow label="permissions" value={permissionSummary} valueWidth={valueWidth} />
        <InfoRow label="get commands" value="/help" valueWidth={valueWidth} />
      </Box>
    </Box>
  );
}

export function formatStartupPermissions(permissions: Record<string, boolean>) {
  if (permissions.unattended || permissions.allow_all) {
    return 'unattended';
  }

  const enabled = [];
  if (permissions.allow_edits) {
    enabled.push('edits');
  }
  if (permissions.allow_run_commands) {
    enabled.push('run commands');
  }

  return enabled.length > 0 ? enabled.join(', ') : 'default';
}

function InfoRow({label, value, valueWidth}: {label: string; value: string; valueWidth: number}) {
  return (
    <Text wrap="truncate-end">
      <Text color={accentColor}>{label}: </Text>
      <Text>{fitText(value, valueWidth)}</Text>
    </Text>
  );
}

function fitText(value: string, maxWidth: number) {
  if (visibleLength(value) <= maxWidth) {
    return value;
  }
  if (maxWidth <= 3) {
    return '.'.repeat(Math.max(0, maxWidth));
  }
  return `${value.slice(0, maxWidth - 3)}...`;
}

function visibleLength(value: string) {
  return [...value].length;
}
