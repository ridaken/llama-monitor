import type { Page } from '@playwright/test'

export const config = {
  name: 'Qwen test',
  model_path: 'C:/Models/qwen.gguf',
  port: 8001,
  log_prompts: false,
  flags: [
    { flag: '-c', value: '4096' },
    { flag: '-fa', value: 'on', enabled: false },
  ],
}

export const launcherState = {
  settings: {
    llama_server_path: 'C:/bin/llama-server.exe',
    models_dir: 'C:/Models',
    default_port: 8001,
    default_config: 'Qwen test',
  },
  binary_valid: true,
  configs: [config],
  status: { state: 'stopped', adopted: false, config_name: null },
  managed_log: 'C:/test/llama-server.log',
}

export const gamingState = {
  enabled: false,
  phase: 'normal',
  blocked: false,
  connected_clients: 0,
  countdown: null,
  integration_error: null,
  servers: [],
  apollo_url: 'https://localhost:47990',
  credentials_saved: false,
  certificate_sha256: null,
  auxiliary: null,
  commands: { prepare: 'prepare-command', 'session-ended': 'undo-command' },
}

export const stats = {
  online: true,
  active: true,
  log_mode: true,
  metrics_enabled: true,
  model: {
    name: 'Qwen test',
    path: 'C:/Models/qwen.gguf',
    n_ctx: 4096,
    total_slots: 2,
  },
  slots: {
    busy: 1,
    total: 2,
    list: [
      {
        id: 0,
        state: 'generating',
        prompt_tokens: 80,
        decoded: 12,
        ctx_ratio: 0.24,
      },
      { id: 1, state: 'idle', ctx_used: 0, ctx_ratio: 0 },
    ],
  },
  requests: { processing: 1, deferred: 2 },
  kv: { usage_ratio: 0.24, tokens: 983 },
  throughput: {
    decode_tps_live: 39.4,
    decode_tps_avg: 35.1,
    pp_tps_avg: 120.2,
  },
  spec: { enabled: true, tokens_per_decode: 2.4 },
  last_request: {
    pp: { tokens: 80, secs: 0.5, tps: 160 },
    generation: { tokens: 12, secs: 0.3, tps: 36.7 },
    total: { tokens: 92, secs: 0.8 },
  },
  split: [
    { label: 'CUDA0', bytes: 10737418240, kind: 'gpu' },
    { label: 'CPU', bytes: 2147483648, kind: 'cpu' },
  ],
  split_source: 'log',
  split_log_configured: true,
  gpu: {
    ok: true,
    devices: [
      {
        index: 0,
        name: 'Test GPU',
        temp: 61,
        util_gpu: 75,
        power: 210,
        power_limit: 300,
        mem_used: 10737418240,
        mem_total: 25769803776,
      },
    ],
  },
  sysmem: {
    ok: true,
    used: 17179869184,
    total: 34359738368,
    percent: 50,
    llama_rss: 2147483648,
  },
}

export const historyPage = {
  items: [
    {
      id: 'h1',
      run_id: 'run-123456',
      slot_id: 0,
      task_id: 7,
      observed_at: 1700000000,
      model: 'Qwen test',
      state: 'complete',
      has_prompt: 1,
      prompt_tokens: 80,
      generated_tokens: 12,
      prompt_seconds: 0.5,
      decode_seconds: 0.3,
      total_seconds: 0.8,
      draft_accepted: 10,
      draft_generated: 12,
    },
  ],
  models: ['Qwen test'],
  next_cursor: null,
  database_path: 'C:/test/history.sqlite',
  log_status: {
    configured: true,
    available: true,
    gap: false,
    prompts_configured: true,
  },
}

export async function mockApi(
  page: Page,
  overrides: Record<string, (request: any) => any> = {},
) {
  const calls: string[] = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    calls.push(`${request.method()} ${path}${url.search}`)
    const custom = overrides[`${request.method()} ${path}`]
    if (custom) {
      const result = await custom(request)
      await route.fulfill({
        status: typeof result?.status === 'number' ? result.status : 200,
        contentType: 'application/json',
        body: JSON.stringify(result?.body ?? result),
      })
      return
    }
    let body: any
    if (path === '/api/stats') body = stats
    else if (path === '/api/gaming/state') body = gamingState
    else if (path === '/api/launcher/state') body = launcherState
    else if (path === '/api/launcher/flags')
      body = {
        source: 'help',
        flags: [
          {
            flags: ['-c', '--ctx-size'],
            desc: 'Context size',
            value_hint: 'N',
          },
          {
            flags: ['-fa', '--flash-attn'],
            desc: 'Flash attention',
            value_hint: 'on/off',
          },
        ],
      }
    else if (path === '/api/history')
      body = request.method() === 'DELETE' ? { deleted: 1 } : historyPage
    else if (path === '/api/history/h1/prompt')
      body = { prompt_text: 'Secret test prompt', truncated: false }
    else if (path === '/api/launcher/console')
      body = {
        available: true,
        content: 'server ready\n',
        offset: 13,
        size: 13,
        path: 'C:/test/llama-server.log',
      }
    else if (path === '/api/browse')
      body = {
        path: url.searchParams.get('path') || 'C:/Models',
        parent: 'C:/',
        dirs: ['C:/Models/sub'],
        files: ['C:/Models/qwen.gguf'],
      }
    else if (path === '/api/configs') body = { configs: [config] }
    else if (path === '/api/configs/default') body = launcherState
    else if (path === '/api/launcher/settings') body = launcherState
    else if (path === '/api/launcher/launch')
      body = {
        ...launcherState,
        status: { state: 'running', config_name: 'Qwen test' },
      }
    else if (path === '/api/launcher/stop') body = launcherState
    else if (path === '/api/launcher/restart')
      body = {
        ...launcherState,
        status: { state: 'running', config_name: 'Qwen test' },
      }
    else body = {}
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(body),
    })
  })
  return calls
}
