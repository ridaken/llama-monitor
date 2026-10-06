export type Flag = { flag: string; value: string; enabled?: boolean }
export type GamingState = {
  enabled: boolean
  reason?: 'startup' | 'gaming' | null
  phase:
    | 'normal'
    | 'stopping'
    | 'gaming'
    | 'countdown'
    | 'restoring'
    | 'retry_countdown'
    | 'failed'
  blocked: boolean
  connected_clients: number | null
  countdown: number | null
  integration_error: string | null
  servers: Array<{
    id: string
    name: string
    port: number
    status: string
    attempts: number
    error?: string | null
  }>
  apollo_url: string
  credentials_saved: boolean
  certificate_sha256: string | null
  auxiliary: { executable: string; port: number } | null
  commands?: { prepare: string; 'session-ended': string }
}
export type StartupState = {
  supported: boolean
  installed: boolean
  mode: 'boot' | 'logon'
  autostart_models: boolean
  models: Array<{ id: string; name: string; port: number }>
  pending: boolean
  last_error: string | null
  last_result: string | null
  task_name: string | null
  log_path: string
}
export type Config = {
  name: string
  model_path: string
  port: number | null
  log_prompts: boolean
  flags: Flag[]
}
export type LauncherState = {
  settings: {
    llama_server_path: string | null
    models_dir: string | null
    default_port: number
    default_config: string | null
  }
  binary_valid: boolean
  configs: Config[]
  status: {
    state: 'running' | 'stopped' | 'exited'
    config_name?: string | null
    config?: Config | null
    exit_code?: number
    adopted?: boolean
  }
  managed_log: string
  gaming?: GamingState
}
export type KnownFlag = {
  flags: string[]
  desc: string
  value_hint?: string | null
}
export type FlagResponse = { source: string; flags: KnownFlag[] }
export type Stats = {
  online: boolean
  gaming?: GamingState
  active?: boolean
  log_mode?: boolean
  metrics_enabled?: boolean
  model?: { name?: string; path?: string; n_ctx?: number; total_slots?: number }
  slots?: {
    busy: number
    total: number
    list: Array<{
      id: number
      state: string
      prompt_tokens?: number
      prompt_processed?: number
      decoded?: number
      ctx_ratio?: number
      prefill_ratio?: number
      ctx_used?: number
    }>
  }
  requests?: { processing?: number; deferred?: number }
  kv?: { usage_ratio?: number | null; tokens?: number | null }
  throughput?: {
    decode_tps_live?: number | null
    decode_tps_avg?: number | null
    pp_tps_avg?: number | null
  }
  spec?: {
    enabled?: boolean
    tokens_per_decode?: number | null
    mean_len?: number | null
    accept_rate?: number | null
  }
  last_request?: { pp?: Timing; generation?: Timing; total?: Timing }
  split?: Array<{ label: string; bytes: number; kind: string }>
  split_source?: string | null
  split_log_configured?: boolean
  gpu?: {
    ok: boolean
    error?: string
    devices?: Array<{
      index: number
      name?: string
      temp?: number | null
      util_gpu?: number | null
      power?: number | null
      power_limit?: number | null
      mem_used?: number
      mem_total?: number
    }>
  }
  sysmem?: {
    ok: boolean
    error?: string
    used?: number
    total?: number
    percent?: number | null
    llama_rss?: number | null
  }
}
export type Timing = { tokens: number; secs: number; tps?: number | null }
export type Activity = {
  id: string
  run_id: string
  slot_id?: number | null
  task_id?: number | null
  observed_at: number
  model?: string
  state: string
  has_prompt?: number | boolean
  prompt_tokens?: number | null
  generated_tokens?: number | null
  prompt_seconds?: number | null
  decode_seconds?: number | null
  total_seconds?: number | null
  draft_accepted?: number | null
  draft_generated?: number | null
}
export type HistoryPage = {
  items: Activity[]
  models: string[]
  next_cursor: string | null
  database_path: string
  log_status: {
    configured: boolean
    available: boolean
    gap: boolean
    prompts_configured?: boolean
  }
}
