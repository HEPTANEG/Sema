




import type { SessionInfo, SlashCategory, Usage } from './types.js'

export interface GatewaySkin {
  banner_hero?: string
  banner_logo?: string
  branding?: Record<string, string>
  colors?: Record<string, string>
  help_header?: string
  tool_prefix?: string
}

export interface GatewayCompletionItem {
  display: string
  meta?: string
  text: string
}

export interface GatewayTranscriptMessage {
  context?: string
  name?: string
  role: 'assistant' | 'system' | 'tool' | 'user'
  text?: string
}



export interface CommandsCatalogResponse {
  canon?: Record<string, string>
  categories?: SlashCategory[]
  pairs?: [string, string][]
  sub?: Record<string, string[]>
  warning?: string
}

export interface CompletionResponse {
  items?: GatewayCompletionItem[]
  replace_from?: number
}

export interface SlashExecResponse {
  output?: string
  warning?: string
}

export type CommandDispatchResponse =
  | { output?: string; type: 'exec' | 'plugin' }
  | { target: string; type: 'alias' }
  | { message?: string; name: string; type: 'skill' }
  | { message: string; notice?: string; type: 'send' }



export interface ConfigDisplayConfig {
  bell_on_complete?: boolean
  busy_input_mode?: string
  details_mode?: string
  inline_diffs?: boolean
  mouse_tracking?: boolean | null | number | string
  sections?: Record<string, string>
  show_cost?: boolean
  show_reasoning?: boolean
  streaming?: boolean
  thinking_mode?: string
  tui_auto_resume_recent?: boolean
  tui_compact?: boolean

  tui_mouse?: boolean | null | number | string





  tui_status_indicator?: string
  tui_statusbar?: 'bottom' | 'off' | 'on' | 'top' | boolean
}

export interface ConfigVoiceConfig {


  record_key?: unknown
}

export interface ConfigFullResponse {
  config?: { display?: ConfigDisplayConfig; voice?: ConfigVoiceConfig }
}

export interface ConfigMtimeResponse {
  mtime?: number
}

export interface ConfigGetValueResponse {
  display?: string
  home?: string
  value?: string
}

export interface ConfigSetResponse {
  applied?: boolean



  applies_to_session?: boolean
  credential_warning?: string
  history_reset?: boolean
  info?: SessionInfo
  previous?: null | string
  scope?: 'default' | 'session'
  session_id?: string
  value?: string
  warning?: string
}

export interface SetupStatusResponse {
  provider_configured?: boolean
}



export interface SessionCreateResponse {
  info?: SessionInfo & { config_notices?: string[]; config_warning?: string; credential_warning?: string }
  session_id: string
}

export interface SessionResumeResponse {
  info?: SessionInfo & { config_notices?: string[] }
  message_count?: number
  messages: GatewayTranscriptMessage[]
  resumed?: string
  session_id: string
}

export interface SessionListItem {
  id: string
  message_count: number
  preview: string
  source?: string
  started_at: number
  title: string
}

export interface SessionListResponse {
  sessions?: SessionListItem[]
}

export interface SessionDeleteResponse {
  deleted: null | string
}

export interface SessionMostRecentResponse {
  session_id?: null | string
  source?: string
  started_at?: number
  title?: string
}

export interface SessionTitleResponse {
  pending?: boolean
  session_key?: string
  title?: string
}

export interface SessionSaveResponse {
  file?: string
}

export interface SessionUndoResponse {
  removed?: number
}

export interface SessionClearResponse {
  session_id: string
  cleared: boolean
}

export interface SessionExportResponse {
  exported: boolean
  path?: string | null
  reason?: string
  candidates?: string[]
}

export interface SessionUsageResponse {
  cache_read?: number
  cache_write?: number
  calls?: number
  compressions?: number
  context_max?: number
  context_percent?: number
  context_used?: number
  cost_status?: 'estimated' | 'exact'
  cost_usd?: number
  input?: number
  model?: string
  output?: number
  total?: number
}

export interface SessionStatusResponse {
  output?: string
}

export interface SessionCompressResponse {
  after_messages?: number
  after_tokens?: number
  before_messages?: number
  before_tokens?: number
  info?: SessionInfo
  messages?: GatewayTranscriptMessage[]
  removed?: number
  summary?: {
    headline?: string
    noop?: boolean
    note?: null | string
    token_line?: string
  }
  usage?: Usage
}

export interface SessionBranchResponse {
  message_count?: number
  session_id?: string
  title?: string
}

export interface SessionCloseResponse {
  ok?: boolean
}

export interface SessionInterruptResponse {
  ok?: boolean
}

export interface SessionSteerResponse {
  status?: 'queued' | 'rejected'
  text?: string
}



export interface PromptSubmitResponse {
  ok?: boolean
}

export interface BackgroundStartResponse {
  task_id?: string
}

export interface ClarifyRespondResponse {
  ok?: boolean
}

export interface ApprovalRespondResponse {
  ok?: boolean
}

export interface SudoRespondResponse {
  ok?: boolean
}

export interface SecretRespondResponse {
  ok?: boolean
}

export interface ConfirmRespondResponse {
  ok?: boolean
}



export interface ShellExecResponse {
  code: number
  stderr?: string
  stdout?: string
}

export interface ClipboardPasteResponse {
  attached?: boolean
  count?: number
  height?: number
  message?: string
  token_estimate?: number
  width?: number
}

export interface InputDetectDropResponse {
  height?: number
  is_image?: boolean
  matched?: boolean
  name?: string
  text?: string
  token_estimate?: number
  width?: number
}

export interface TerminalResizeResponse {
  ok?: boolean
}



export interface ImageAttachResponse {
  height?: number
  name?: string
  remainder?: string
  token_estimate?: number
  width?: number
}



export interface VoiceToggleResponse {
  audio_available?: boolean
  available?: boolean
  details?: string
  enabled?: boolean
  record_key?: string
  stt_available?: boolean
  tts?: boolean
}

export interface VoiceRecordResponse {
  status?: 'busy' | 'recording' | 'stopped'
  text?: string
}



export interface ToolsConfigureResponse {
  changed?: string[]
  enabled_toolsets?: string[]
  info?: SessionInfo
  missing_servers?: string[]
  reset?: boolean
  unknown?: string[]
}









export interface ModelOptionProvider {
  auth_type?: string
  authenticated?: boolean
  is_current?: boolean
  key_env?: null | string
  model_labels?: Record<string, { description?: string; label: string }>
  models?: string[]
  name: string
  needs_api_base?: boolean
  slug: string
  total_models?: number
  warning?: string
}

export interface ModelOptionsResponse {
  model?: string
  provider?: string
  providers?: ModelOptionProvider[]
}




export interface ProviderEndpointInfo {
  api_base?: null | string
  api_key?: string
  extra_headers?: null | Record<string, string>
  label: string
}

export interface ModelEndpointsResponse {
  endpoints?: ProviderEndpointInfo[]
}

export interface ReloadEnvResponse {
  updated?: number
}

export interface ProcessStopResponse {
  killed?: number
}

export interface BrowserManageResponse {
  connected?: boolean
  messages?: string[]
  url?: string
}

export interface RollbackCheckpoint {
  hash: string
  message?: string
  timestamp?: string
}

export interface RollbackListResponse {
  checkpoints?: RollbackCheckpoint[]
  enabled?: boolean
}

export interface RollbackDiffResponse {
  diff?: string
  rendered?: string
  stat?: string
}

export interface RollbackRestoreResponse {
  error?: string
  history_removed?: number
  message?: string
  reason?: string
  restored_to?: string
  success?: boolean
}



export interface SubagentEventPayload {
  api_calls?: number
  cost_usd?: number
  depth?: number
  duration_seconds?: number
  files_read?: string[]
  files_written?: string[]
  goal: string
  input_tokens?: number
  iteration?: number
  model?: string
  output_tail?: { is_error?: boolean; preview?: string; tool?: string }[]
  output_tokens?: number
  parent_id?: null | string
  reasoning_tokens?: number
  status?: 'completed' | 'failed' | 'interrupted' | 'queued' | 'running'
  subagent_id?: string
  summary?: string
  task_count?: number
  task_index: number
  text?: string
  tool_count?: number
  tool_name?: string
  tool_preview?: string
  toolsets?: string[]
}



export interface DelegationStatusResponse {
  active?: {
    depth?: number
    goal?: string
    model?: null | string
    parent_id?: null | string
    started_at?: number
    status?: string
    subagent_id?: string
    tool_count?: number
  }[]
  max_concurrent_children?: number
  max_spawn_depth?: number
  paused?: boolean
}

export interface DelegationPauseResponse {
  paused?: boolean
}

export interface SubagentInterruptResponse {
  found?: boolean
  subagent_id?: string
}



export interface SpawnTreeListEntry {
  count: number
  finished_at?: number
  label?: string
  path: string
  session_id?: string
  started_at?: number | null
}

export interface SpawnTreeListResponse {
  entries?: SpawnTreeListEntry[]
}

export interface SpawnTreeLoadResponse {
  finished_at?: number
  label?: string
  session_id?: string
  started_at?: null | number
  subagents?: unknown[]
}

export type GatewayEvent =
  | { payload?: { skin?: GatewaySkin }; session_id?: string; type: 'gateway.ready' }
  | { payload?: GatewaySkin; session_id?: string; type: 'skin.changed' }
  | { payload: SessionInfo; session_id?: string; type: 'session.info' }
  | { payload?: { text?: string }; session_id?: string; type: 'thinking.delta' }
  | { payload?: undefined; session_id?: string; type: 'message.start' }
  | { payload?: { index?: number }; session_id?: string; type: 'episode.start' }
  | { payload?: { kind?: string; text?: string }; session_id?: string; type: 'status.update' }
  | { payload?: { state?: 'idle' | 'listening' | 'transcribing' }; session_id?: string; type: 'voice.status' }
  | { payload?: { no_speech_limit?: boolean; text?: string }; session_id?: string; type: 'voice.transcript' }
  | { payload: { line: string }; session_id?: string; type: 'gateway.stderr' }
  | {
      payload?: { level?: 'info' | 'warn' | 'error'; message?: string }
      session_id?: string
      type: 'browser.progress'
    }
  | {
      payload?: { cwd?: string; python?: string; stderr_tail?: string }
      session_id?: string
      type: 'gateway.start_timeout'
    }
  | { payload?: { preview?: string }; session_id?: string; type: 'gateway.protocol_error' }
  | { payload?: { text?: string }; session_id?: string; type: 'reasoning.delta' | 'reasoning.available' }
  | { payload: { name?: string; preview?: string }; session_id?: string; type: 'tool.progress' }
  | { payload: { name?: string }; session_id?: string; type: 'tool.generating' }
  | {
      payload: { context?: string; name?: string; tool_id: string; todos?: unknown[] }
      session_id?: string
      type: 'tool.start'
    }
  | {
      payload: {
        duration_s?: number
        error?: string
        inline_diff?: string
        name?: string
        summary?: string
        tool_id: string
        todos?: unknown[]
      }
      session_id?: string
      type: 'tool.complete'
    }
  | {
      payload: { choices: string[] | null; question: string; request_id: string }
      session_id?: string
      type: 'clarify.request'
    }
  | {
      payload: {
        approval_id: string
        command: string
        conversation_id: string
        description: string
        expires_at: number
        tool_call_id: string
        turn_id: string
      }
      session_id?: string
      type: 'approval.request'
    }
  | {
      payload: {
        approval_id: string
        conversation_id: string
        reason: string
      }
      session_id?: string
      type: 'approval.closed'
    }
  | { payload: { request_id: string }; session_id?: string; type: 'sudo.request' }
  | { payload: { env_var: string; prompt: string; request_id: string }; session_id?: string; type: 'secret.request' }
  | { payload: { default: boolean; prompt: string; request_id: string }; session_id?: string; type: 'confirm.request' }
  | { payload: { task_id: string; text: string }; session_id?: string; type: 'background.complete' }
  | {
      payload?: Record<string, unknown> & { run_id?: string; start_status?: string }
      session_id?: string
      type: 'ltm_maintenance_started'
    }
  | {
      payload?: Record<string, unknown> & { run_id?: string; stage?: string; stage_index?: number }
      session_id?: string
      type: 'ltm_maintenance_stage_started'
    }
  | {
      payload?: Record<string, unknown> & { run_id?: string; stage?: string; stage_index?: number; status?: string; stage_summary?: string; token_count?: number }
      session_id?: string
      type: 'ltm_maintenance_stage_completed'
    }
  | {
      payload?: Record<string, unknown> & { run_id?: string }
      session_id?: string
      type: 'ltm_maintenance_model_turn'
    }
  | {
      payload?: Record<string, unknown> & {
        diagnostics?: unknown
        error?: string
        max_retries?: number
        retry?: number
        run_id?: string
      }
      session_id?: string
      type: 'ltm_maintenance_model_error'
    }
  | {
      payload?: Record<string, unknown> & { content?: string; run_id?: string }
      session_id?: string
      type: 'ltm_maintenance_reasoning_available' | 'ltm_maintenance_message_delta'
    }
  | {
      payload?: Record<string, unknown> & { content?: string; run_id?: string; repeated_rounds?: number }
      session_id?: string
      type: 'ltm_maintenance_repetition_warning' | 'ltm_maintenance_runtime_notice'
    }
  | {
      payload?: Record<string, unknown> & { args?: unknown; item_id?: string; run_id?: string; tool?: string }
      session_id?: string
      type: 'ltm_maintenance_tool_started'
    }
  | {
      payload?: Record<string, unknown> & {
        item_id?: string
        result?: unknown
        run_id?: string
        status?: string
        tool?: string
      }
      session_id?: string
      type: 'ltm_maintenance_tool_finished' | 'ltm_maintenance_plan_committed' | 'ltm_maintenance_coverage_advanced'
    }
  | {
      payload?: Record<string, unknown> & { run_id?: string }
      session_id?: string
      type: 'ltm_maintenance_final_check_started'
    }
  | {
      payload?: Record<string, unknown> & { health?: unknown; run_id?: string }
      session_id?: string
      type: 'ltm_maintenance_final_check_failed'
    }
  | {
      payload?: Record<string, unknown> & { run_id?: string; status?: string }
      session_id?: string
      type: 'ltm_maintenance_completed' | 'ltm_maintenance_failed'
    }
  | {
      payload: { fired_at: string; job_id: string; name: string; text: string }
      session_id?: string
      type: 'cron.delivered'
    }
  | { payload?: { text?: string }; session_id?: string; type: 'review.summary' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.spawn_requested' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.start' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.thinking' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.tool' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.progress' }
  | { payload: SubagentEventPayload; session_id?: string; type: 'subagent.complete' }
  | { payload: { rendered?: string; text?: string }; session_id?: string; type: 'message.delta' }
  | {
      payload?: { reasoning?: string; rendered?: string; text?: string; usage?: Usage }
      session_id?: string
      type: 'message.complete'
    }
  | { payload?: { message?: string }; session_id?: string; type: 'error' }
  | { payload: { wake_id?: string; source?: string; kind?: string; processing_class?: string }; session_id?: string; type: 'wake_delivered' }
  | { payload: { wake_id?: string; attempt?: number; reason?: string }; session_id?: string; type: 'wake_deferred' }
  | { payload: { wake_id?: string; error?: string; attempt?: number; reason?: string }; session_id?: string; type: 'wake_failed' }
