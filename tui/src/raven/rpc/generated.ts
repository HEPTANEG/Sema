

















export type JsonValue = string | number | boolean | null | unknown[] | {};






export type TurnEvent =
  | MessageStartEvent
  | EpisodeStartEvent
  | TokenDeltaEvent
  | ThinkingDeltaEvent
  | ToolStartEvent
  | ToolProgressEvent
  | ToolCompleteEvent
  | MessageCompleteEvent
  | ErrorEvent
  | CronDeliveredEvent
  | CronMissedEvent;





export interface SessionInfo {



  session_key: string;
  channel: string;
  chat_id: string;



  created_at: string;



  updated_at: string;
  message_count: number;
  metadata: {
    [k: string]: JsonValue;
  };
  has_pending_clarification: boolean;
}






export interface SessionListItem {



  id: string;
  message_count: number;
  preview: string;
  source?: string;



  started_at: number;
  title: string;
}




export interface SessionMessage {



  index: number;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;



  timestamp: string;
  metadata?: {
    [k: string]: JsonValue;
  };
}




export interface ModelOptionProvider {
  slug: string;
  name: string;
  authenticated: boolean;
  is_current: boolean;
  auth_type: string;
  key_env?: string;
  models: string[];
  total_models: number;
  needs_api_base: boolean;
  warning: string;



  model_labels?: {
    [k: string]: ModelLabel;
  };
}






export interface ModelLabel {
  label: string;
  description?: string;
}






export interface ProviderEndpointInfo {
  label: string;



  api_key: string;
  api_base?: string;
  extra_headers?: {
    [k: string]: string;
  };
}




export interface UsageSnapshot {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd?: number;
  context_used?: number;
  context_max?: number;
  context_percent?: number;
}







export interface CliResult {



  stdout: string;



  stderr: string;



  exit_code: number;



  error_code?: number;
}


































export interface StubResult {



  error: string;



  hint?: string;
}







export interface CommandsCatalogResponse {



  canon: {
    [k: string]: string;
  };



  pairs: [unknown, unknown][];



  sub: {
    [k: string]: string[];
  };



  categories: string[];



  warning?: string;
}




export interface MessageStartEvent {
  type: 'message.start';
  payload: {
    turn_id: string;
  };
}




export interface EpisodeStartEvent {
  type: 'episode.start';
  payload: {
    index: number;
  };
}




export interface TokenDeltaEvent {
  type: 'token.delta';
  payload: {
    text: string;
  };
}




export interface ThinkingDeltaEvent {
  type: 'thinking.delta';
  payload: {
    text: string;
  };
}




export interface ToolStartEvent {
  type: 'tool.start';
  payload: {
    tool_call_id: string;
    name: string;
    arguments: {
      [k: string]: JsonValue;
    };
    display?: string | null;
  };
}




export interface ToolProgressEvent {
  type: 'tool.progress';
  payload: {
    tool_call_id: string;
    preview: string;
  };
}




export interface ToolCompleteEvent {
  type: 'tool.complete';
  payload: {
    tool_call_id: string;
    result_preview: string;
    truncated: boolean;
  };
}




export interface MessageCompleteEvent {
  type: 'message.complete';
  payload: {
    turn_id: string;
    usage: UsageSnapshot;
  };
}




export interface ErrorEvent {
  type: 'error';
  payload: {
    code: number;
    message: string;
    reason?: 'cancelled_by_client' | 'internal';
    detail?: string;
  };
}




export interface CronDeliveredEvent {
  type: 'cron.delivered';
  payload: {
    job_id: string;
    name: string;
    text: string;
    fired_at: string;
  };
}




export interface CronMissedEvent {
  type: 'cron.missed';
  payload: {
    count: number;
    items: {
      name: string;
      scheduled_at: string;
      message: string;
    }[];
  };
}




export interface SessionListParams {



  limit?: number;
}




export interface SessionListResult {
  sessions: SessionListItem[];
}




export interface SessionGetParams {
  session_key: string;
}




export interface SessionGetResult {
  session: SessionInfo;
}




export interface SessionCreateParams {
  channel: string;
  chat_id: string;
  metadata?: {
    [k: string]: JsonValue;
  };
}




export interface SessionCreateResult {
  session: SessionInfo;
}




export interface SessionResumeParams {
  session_key: string;
}




export interface SessionResumeResult {
  session: SessionInfo;
  last_messages: SessionMessage[];
}




export interface SessionDeleteParams {



  session_id: string;
}




export interface SessionDeleteResult {



  deleted?: string;
}




export interface SessionMostRecentParams {}




export interface SessionMostRecentResult {



  session_id?: string;
  source?: string;
  started_at?: number;
  title?: string;
}




export interface SessionTitleParams {



  session_id: string;



  title?: string;
}




export interface SessionTitleResult {
  title?: string;
  session_key: string;



  pending: boolean;
}




export interface SessionClearParams {



  session_id: string;
}




export interface SessionClearResult {



  session_id: string;



  cleared: boolean;
}




export interface SessionUndoParams {



  session_id: string;



  n?: number;
}




export interface SessionUndoResult {



  removed: number;
}




export interface SessionExportParams {



  session_id?: string;
}




export interface SessionExportResult {



  exported: boolean;



  path?: string;



  reason?: string;



  candidates?: string[];
}




export interface SessionHistoryParams {
  session_key: string;



  max_messages?: number;



  before_index?: number;
}




export interface SessionHistoryResult {
  messages: SessionMessage[];
  total: number;
}




export interface TurnSendParams {
  session_key: string;
  content: string;
  channel?: string;
  chat_id?: string;
  sender_id?: string;



  media?: string[];
}




export interface TurnSendResult {
  turn_id: string;
  accepted: boolean;
}




export interface TurnSubscribeParams {
  session_key: string;
}




export interface TurnSubscribeResult {
  subscription_id: string;
}




export interface TurnUnsubscribeParams {
  subscription_id: string;
}




export interface TurnUnsubscribeResult {
  unsubscribed: boolean;
}




export interface TurnCancelParams {
  session_key: string;
}




export interface TurnCancelResult {
  cancelled: boolean;
}




export interface ModelOptionsParams {
  session_id?: string;
}




export interface ModelOptionsResult {
  model: string;
  provider: string;
  providers: ModelOptionProvider[];
}




export interface ModelSaveKeyParams {
  slug: string;
  api_key?: string;
  api_base?: string;
  session_id?: string;
}




export interface ModelSaveKeyResult {
  provider: ModelOptionProvider;
}




export interface ModelDisconnectParams {
  slug: string;
  session_id?: string;
}




export interface ModelDisconnectResult {
  disconnected: boolean;
}




export interface ModelAddModelParams {
  slug: string;
  model: string;
  session_id?: string;
}




export interface ModelAddModelResult {
  provider: ModelOptionProvider;
}




export interface ModelRemoveModelParams {
  slug: string;
  model: string;
  session_id?: string;
}




export interface ModelRemoveModelResult {
  provider: ModelOptionProvider;
}




export interface ModelEndpointsParams {
  slug: string;
  session_id?: string;
}




export interface ModelEndpointsResult {
  endpoints: ProviderEndpointInfo[];
}




export interface ModelAddEndpointParams {
  slug: string;
  label: string;
  api_key?: string;
  api_base?: string;
  session_id?: string;
}




export interface ModelAddEndpointResult {
  endpoints: ProviderEndpointInfo[];
}




export interface ModelRemoveEndpointParams {
  slug: string;
  label: string;
  session_id?: string;
}




export interface ModelRemoveEndpointResult {
  endpoints: ProviderEndpointInfo[];
}




export interface ConfigGetParams {



  keys?: string[];
}




export interface ConfigGetResult {
  config: {
    [k: string]: JsonValue;
  };
}




export interface ConfigSetParams {
  key: string;
  value: JsonValue;
  session_id?: string;
  provider?: string;
  scope?: 'session' | 'default';
}




export interface ConfigSetResult {
  applied: boolean;
  previous: JsonValue | null;
  value?: string;
  scope?: 'session' | 'default';
  session_id?: string;
  applies_to_session?: boolean;
}




export interface SystemHelloParams {
  client_version: string;
  client_capabilities?: string[];
}




export interface SystemHelloResult {
  server_version: string;
  server_capabilities: string[];
  session: {
    default_channel: 'tui';
    default_session_key: string;
  };
}




export interface SystemPingParams {}




export interface SystemPingResult {
  pong: true;
  server_time_ms: number;
}




export interface SystemVersionParams {}




export interface SystemVersionResult {
  server_version: string;



  schema_version: string;
  raven_version: string;
}




export interface CliDispatchParams {



  argv: string[];



  width: number;



  timeout_s?: number;
}




export interface SetupStatusParams {}




export interface SetupStatusResult {
  provider_configured: boolean;
}




export interface CommandsCatalogParams {}




export interface VoiceToggleParams {
  action?: string;
}




export interface BrowserManageParams {
  action?: string;
  url?: string;
}




export interface SpawnTreeSaveParams {
  name?: string;
}




export interface SpawnTreeListParams {}




export interface SpawnTreeLoadParams {
  name?: string;
}




export interface ProcessStopParams {}




export interface RollbackListParams {}




export interface RollbackDiffParams {
  id?: string;
}




export interface RollbackRestoreParams {
  id?: string;
}




export interface ToolsConfigureParams {}


export type BrowserManageResult = StubResult;
export type CliDispatchResult = CliResult;
export type CommandsCatalogResult = CommandsCatalogResponse;
export type ProcessStopResult = StubResult;
export type RollbackDiffResult = StubResult;
export type RollbackListResult = StubResult;
export type RollbackRestoreResult = StubResult;
export type SpawnTreeListResult = StubResult;
export type SpawnTreeLoadResult = StubResult;
export type SpawnTreeSaveResult = StubResult;
export type ToolsConfigureResult = StubResult;
export type VoiceToggleResult = StubResult;





export interface JsonRpcRequest<P = unknown> {
  jsonrpc: '2.0';
  id: string | number;
  method: string;
  params: P;
}

export interface JsonRpcSuccess<R = unknown> {
  jsonrpc: '2.0';
  id: string | number;
  result: R;
}

export interface JsonRpcErrorObject {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcErrorResponse {
  jsonrpc: '2.0';
  id: string | number;
  error: JsonRpcErrorObject;
}

export type JsonRpcResponse<R = unknown> = JsonRpcSuccess<R> | JsonRpcErrorResponse;

export interface JsonRpcNotification<P = unknown> {
  jsonrpc: '2.0';
  method: string;
  params: P;
}

export interface EventNotificationParams<E = unknown> {
  subscription_id: string;
  event: E;
}

export function isJsonRpcError<R>(
  resp: JsonRpcResponse<R>,
): resp is JsonRpcErrorResponse {
  return (resp as JsonRpcErrorResponse).error !== undefined;
}
