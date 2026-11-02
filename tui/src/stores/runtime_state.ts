import {atom, map} from 'nanostores';
import type {ConfirmationRequest, ContextPressureTelemetry, EditStats, SlashCommand, ToolRecord} from '../protocol/types.js';

export type ActiveView = 'chat' | 'config' | 'settings' | 'setup' | 'confirm' | 'commandOutput';
export const $setup = atom<Record<string, any> | null>(null);
export const $setupPage = atom('overview');
export const $configReturnView = atom<'chat' | 'settings' | 'setup'>('chat');

export type TranscriptItem = {
  id: string;
  sequence?: number;
  turnId?: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  state?: 'streaming' | 'running' | 'complete';
  tool?: ToolRecord;
  stage?: boolean;
  provisional?: boolean;
  markdown?: boolean;
};

export type StartupLogItem = {
  id: string;
  role: 'startup';
};

export type StaticLogItem = TranscriptItem | StartupLogItem;

export type BackgroundActivity = {
  id: string;
  kind: 'background';
  label: string;
  startedAt: number;
};

export const $initialized = atom(false);
export const $status = atom('starting');
export const $model = atom('');
export const $continuedSession = atom<boolean | null>(null);
export const $bridge = map<Record<string, unknown>>({});
export const $project = map<Record<string, unknown>>({});
export const $permissions = map<Record<string, boolean>>({});
export const $notifications = map<Record<string, unknown>>({enabled: true});
export const $slashCommands = atom<SlashCommand[]>([]);
export const $staticLogItems = atom<StaticLogItem[]>([]);
export const $liveItems = atom<TranscriptItem[]>([]);
export const $streamingAssistantBuffers = atom<Record<string, string>>({});
export const $backgroundActivities = atom<BackgroundActivity[]>([]);
export const $foregroundStatus = atom('');
export const $turnCodeChanges = atom<EditStats>({added: 0, removed: 0, files: []});
export const $contextPressure = atom<ContextPressureTelemetry | null>(null);
export const $transcript = atom<TranscriptItem[]>([]);
export const $tools = atom<ToolRecord[]>([]);
export const $pendingConfirmation = atom<ConfirmationRequest | null>(null);
export const $lastError = atom('');
export const $config = atom<Record<string, unknown> | null>(null);
export const $settings = atom<Record<string, unknown> | null>(null);
export const $busy = atom(false);
export const $operationStartedAt = atom<number | null>(null);
export const $turnStartedAt = atom<number | null>(null);
export const $lastTurnDurationMs = atom<number | null>(null);
export const $lastEventAt = atom<number | null>(null);
export const $turnStatus = atom<'idle' | 'running'>('idle');
export const $cancelRequested = atom(false);
export const $activeView = atom<ActiveView>('chat');
export const $commandOutput = atom<{title: string; content: string} | null>(null);
export const $panelScroll = atom(0);
export const $pendingUserMessage = atom<{id: string; content: string} | null>(null);
export const $terminalColumns = atom(80);
export const $terminalRows = atom(24);
export const $transcriptScroll = atom(0);
export const $transcriptContentHeight = atom(0);
export const $transcriptViewportHeight = atom(1);
export const $transcriptFollow = atom(true);
