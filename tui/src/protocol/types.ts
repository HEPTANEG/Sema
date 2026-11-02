export type JsonRpcId = string | number;

export type JsonRpcResponse<T = unknown> = {
  jsonrpc: '2.0';
  id: JsonRpcId;
  result?: T;
  error?: {
    code: number;
    message: string;
    data?: unknown;
  };
};

export type BridgeEventType =
  | 'turn_started'
  | 'user_message'
  | 'intent_routing_started'
  | 'intent_routing_finished'
  | 'assistant_stage'
  | 'assistant_delta'
  | 'assistant_content'
  | 'final_answer'
  | 'model_thinking'
  | 'model_finished'
  | 'model_error'
  | 'tool_started'
  | 'tool_finished'
  | 'confirmation_requested'
  | 'reviewer_started'
  | 'reviewer_finished'
  | 'reviewer_feedback'
  | 'coordinator_state_updating'
  | 'coordinator_state_updated'
  | 'context_pressure'
  | 'working_memory_preflight_started'
  | 'working_memory_preflight_finished'
  | 'focus_detector_started'
  | 'focus_detector_finished'
  | 'focus_shifted'
  | 'working_memory_maintenance_started'
  | 'working_memory_maintenance_finished'
  | 'segment_ingest_started'
  | 'segment_ingest_finished'
  | 'full_segment_maintenance_started'
  | 'full_segment_maintenance_finished'
  | 'retriever_started'
  | 'retriever_finished'
  | 'memory_push_started'
  | 'memory_push_finished'
  | 'ltm_formation_started'
  | 'ltm_formation_finished'
  | 'formation_backfill_started'
  | 'formation_backfill_finished'
  | 'memorize_started'
  | 'memorize_finished'
  | 'wm_ingress_watermark_status'
  | 'working_memory_status'
  | 'focus_segment_status'
  | 'ltm_formation_status'
  | 'cancel_requested'
  | 'turn_cancelled'
  | 'agent_stopped'
  | 'agent_error'
  | 'error';

export type BridgeEventPayloadMap = {
  user_message: {content: string};
  assistant_stage: {label: string; message_id?: string};
  assistant_delta: {delta: string; message_id?: string};
  assistant_content: {content: string; message_id?: string};
  final_answer: {content: string; message_id?: string; path?: string; source?: string};
  model_error: {error: string; retry?: number};
  tool_started: {tool: string; args?: unknown; item_id?: string};
  tool_finished: {tool: string; status: 'ok' | 'error'; result?: string; error?: string; item_id?: string};
  confirmation_requested: {confirmation_id: string; choices: ConfirmationChoice[]};
  turn_cancelled: {message: string};
  agent_stopped: {message: string};
  agent_error: {error: string};
  error: {message: string};
  [event: string]: Record<string, unknown>;
};

export type BridgeEvent<T = unknown> = {
  event_id: string;
  turn_id: string;
  type: string;
  sequence?: number;
  payload: T;
};

export type TypedBridgeEvent<K extends BridgeEventType> = {
  event_id: string;
  turn_id: string;
  type: K;
  sequence?: number;
  payload: BridgeEventPayloadMap[K];
};

export type SlashCommand = {
  name: string;
  help: string;
};

export type ConfirmationChoice = {
  id: string;
  label: string;
  requires_reason?: boolean;
  local_preview?: boolean;
};

export type ConfirmationRequest = {
  confirmation_id: string;
  kind: string;
  title: string;
  details: string;
  choices: ConfirmationChoice[];
  preview: string;
};

export type ToolRecord = {
  id: string;
  turn_id: string;
  sequence?: number;
  tool: string;
  args: unknown;
  status: 'running' | 'ok' | 'error';
  result: string;
  error?: string;
  edit_stats?: EditStats;
};

export type EditStats = {
  added: number;
  removed: number;
  files?: string[];
  tool?: string;
};

export type ContextPressureTelemetry = {
  budget_source?: string;
  stm?: number;
  stm_min?: number;
  stm_max?: number;
  wm?: number;
  wm_min?: number;
  wm_max?: number;
  I?: number;
  S?: number;
  R?: number;
  L?: number;
  P_context?: number;
};

export type InitializeResult = {
  project: Record<string, unknown>;
  model: string;
  bridge?: {
    model_timeout_seconds?: number;
  };
  continued_session: boolean;
  permissions: Record<string, boolean>;
  notifications?: Record<string, unknown>;
  slash_commands: SlashCommand[];
  context_pressure?: ContextPressureTelemetry;
};

export type SessionState = {
  project: Record<string, unknown>;
  model: string;
  continued_session: boolean;
  permissions: Record<string, boolean>;
  last_run: unknown[];
  task_state: string;
  turn_status: 'idle' | 'running';
  active_turn_id: string;
  turn_started_at: number | null;
  last_event_at: number | null;
  last_event_type: string;
  last_event_payload_summary: string;
  cancel_requested: boolean;
  elapsed_seconds: number;
  idle_seconds: number;
};

export type LastRunResult = {
  steps: Array<{
    tool?: string;
    args?: unknown;
    result?: string;
    error?: string;
    duration_ms?: number;
  }>;
};

export type TrajectoryListResult = {
  trajectories: Array<Record<string, unknown>>;
  path: string;
  trajectory_id: string;
};
