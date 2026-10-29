import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, post } from '@/api'
import { basename } from '@/lib/utils'
import type {
  Config,
  DoctorReport,
  Flag,
  FlagResponse,
  KnownFlag,
  LauncherState,
  LogRun,
  Stats,
} from '@/types'

type Modal =
  | null
  | 'save-as'
  | 'delete'
  | 'switch'
  | 'browse-model'
  | 'browse-binary'
  | 'console'
type Browse = {
  path: string
  parent: string | null
  dirs: string[]
  files: string[]
  error?: string
}
const blank = (port: number): Config => ({
  name: '',
  model_path: '',
  port,
  log_prompts: false,
  flags: [],
})
const normalizeConfig = (config: Config, defaultPort: number): Config => ({
  name: config.name || '',
  model_path: config.model_path || '',
  port: config.port || defaultPort,
  log_prompts: !!config.log_prompts,
  flags: Array.isArray(config.flags)
    ? config.flags.map((flag) => ({
        flag: flag.flag || '',
        value: flag.value || '',
        ...(flag.enabled === false ? { enabled: false } : {}),
      }))
    : [],
})
const canonical = (cfg: Config) =>
  JSON.stringify({
    name: cfg.name.trim(),
    model_path: cfg.model_path.trim(),
    port: cfg.port,
    log_prompts: !!cfg.log_prompts,
    flags: cfg.flags
      .filter((f) => f.flag.trim())
      .map((f) => ({
        flag: f.flag.trim(),
        value: f.value.trim(),
        enabled: f.enabled !== false,
      })),
  })
const cleanFlag = (flag: Flag) => ({
  flag: flag.flag.trim(),
  value: flag.value.trim(),
  ...(flag.enabled === false ? { enabled: false } : {}),
})
export function useLauncher() {
  const [state, setState] = useState<LauncherState | null>(null)
  const [savingAutostart, setSavingAutostart] = useState(false)
  const [autostartSaveError, setAutostartSaveError] = useState('')
  const [knownFlags, setKnownFlags] = useState<KnownFlag[]>([])
  const [flagSource, setFlagSource] = useState('bundled')
  const [loaded, setLoaded] = useState<Config | null>(null)
  const [draft, setDraft] = useState<Config>(blank(8001))
  const [message, setMessage] = useState<{
    text: string
    bad?: boolean
    target?: 'configuration' | 'launch' | 'binary'
  }>({
    text: '',
  })
  const [modal, setModal] = useState<Modal>(null)
  const [targetConfig, setTargetConfig] = useState('')
  const [switchAfterSaveAs, setSwitchAfterSaveAs] = useState<string | null>(
    null,
  )
  const [newName, setNewName] = useState('')
  const [flagSearch, setFlagSearch] = useState('')
  const [flagOpen, setFlagOpen] = useState(false)
  const [flagSelection, setFlagSelection] = useState(0)
  const [flagsCollapsed, setFlagsCollapsed] = useState(true)
  const [browse, setBrowse] = useState<Browse | null>(null)
  const [browseLoading, setBrowseLoading] = useState(false)
  const [consoleText, setConsoleText] = useState('')
  const [consolePath, setConsolePath] = useState('')
  const [consoleOffset, setConsoleOffset] = useState(0)
  const [consoleRuns, setConsoleRuns] = useState<LogRun[]>([])
  const [consoleRun, setConsoleRun] = useState('')
  const [consoleRunCursor, setConsoleRunCursor] = useState<string | null>(null)
  const [consoleLogError, setConsoleLogError] = useState('')
  const consoleCursorRef = useRef('')
  const loadConsoleRuns = useCallback(async (cursor = '', refresh = false) => {
    try {
      const result = await api<{ items: LogRun[]; next_cursor: string | null }>(
        `/api/launcher/log-runs?limit=50&cursor=${encodeURIComponent(cursor)}`,
      )
      setConsoleRuns((previous) => {
        if (cursor) return [...previous, ...result.items]
        if (!refresh) return result.items
        const ids = new Set(result.items.map((run) => run.run_id))
        return [
          ...result.items,
          ...previous.filter((run) => !ids.has(run.run_id)),
        ]
      })
      if (!refresh) setConsoleRunCursor(result.next_cursor)
    } catch (error) {
      setConsoleLogError((error as Error).message)
    }
  }, [])
  const consoleSourceRef = useRef('')
  const [diagnostics, setDiagnostics] = useState<DoctorReport | null>(null)
  const [diagnosticsError, setDiagnosticsError] = useState('')
  const [diagnosing, setDiagnosing] = useState(false)
  const diagnosticRequest = useRef(0)
  const runDiagnostics = useCallback(async () => {
    const request = ++diagnosticRequest.current
    setDiagnosing(true)
    setDiagnostics(null)
    setDiagnosticsError('')
    try {
      const result = await api<DoctorReport>('/api/launcher/doctor')
      if (request === diagnosticRequest.current) setDiagnostics(result)
    } catch (error) {
      if (request === diagnosticRequest.current)
        setDiagnosticsError(
          `Could not run diagnostics: ${(error as Error).message}`,
        )
    } finally {
      if (request === diagnosticRequest.current) setDiagnosing(false)
    }
  }, [])
  useEffect(() => {
    if (state?.status.state === 'exited') runDiagnostics()
    else {
      ++diagnosticRequest.current
      setDiagnostics(null)
      setDiagnosticsError('')
      setDiagnosing(false)
    }
    return () => {
      ++diagnosticRequest.current
    }
  }, [state?.status.state, state?.status.started_at, runDiagnostics])
  const consoleOffsetRef = useRef(0)
  const consoleHasRef = useRef(false)
  const consolePreRef = useRef<HTMLPreElement>(null)
  const deleteButtonRef = useRef<HTMLButtonElement>(null)
  const consoleStickRef = useRef(true)
  const initialized = useRef(false)
  const stateRef = useRef<LauncherState | null>(null)
  const dirty = loaded
    ? canonical(draft) !== canonical(loaded)
    : canonical(draft) !==
      canonical(blank(state?.settings.default_port || 8001))
  const baseline = loaded || blank(state?.settings.default_port || 8001)
  const changed = {
    model: draft.model_path.trim() !== baseline.model_path.trim(),
    port: draft.port !== baseline.port,
    prompts: draft.log_prompts !== baseline.log_prompts,
    name: draft.name.trim() !== baseline.name.trim(),
    flags: JSON.stringify(draft.flags) !== JSON.stringify(baseline.flags),
  }

  const update = (changes: Partial<Config>) =>
    setDraft((previous) => ({ ...previous, ...changes }))
  const notify = (text: string, bad = false) => setMessage({ text, bad })
  const applyState = (next: LauncherState) => {
    stateRef.current = next
    setState(next)
  }
  const loadConfig = (
    name: string,
    available = stateRef.current?.configs || [],
  ) => {
    const found = available.find((c) => c.name === name) || null
    const normalized = found
      ? normalizeConfig(found, stateRef.current?.settings.default_port || 8001)
      : null
    setLoaded(normalized ? structuredClone(normalized) : null)
    setDraft(
      normalized
        ? structuredClone(normalized)
        : blank(stateRef.current?.settings.default_port || 8001),
    )
    setMessage({ text: '' })
  }
  const loadFlags = useCallback(async () => {
    try {
      const result = await api<FlagResponse>('/api/launcher/flags')
      setKnownFlags(result.flags || [])
      setFlagSource(result.source)
    } catch {
      setKnownFlags([])
      setFlagSource('bundled')
    }
  }, [])

  useEffect(() => {
    let live = true
    let pending = false
    const begin = async () => {
      if (pending) return
      pending = true
      try {
        const current = await api<LauncherState>('/api/launcher/state')
        if (!live) return
        applyState(current)
        if (!initialized.current) {
          initialized.current = true
          const running = current.status.state === 'running'
          const pendingRestore = current.gaming?.servers.some(
            (server) =>
              server.id === 'primary' && server.status !== 'cancelled',
          )
          const runningConfig =
            running || pendingRestore ? current.status.config : null
          if (runningConfig) {
            // Keep the saved config as the baseline so launch-only edits can
            // still be saved, but restore what this server actually launched.
            loadConfig(runningConfig.name || '', current.configs)
            setDraft(
              normalizeConfig(
                runningConfig,
                current.settings.default_port || 8001,
              ),
            )
            notify('Restored the running server configuration.')
            return
          }
          let choice =
            running &&
            current.configs.some((c) => c.name === current.status.config_name)
              ? current.status.config_name || ''
              : running
                ? ''
                : current.settings.default_config || ''
          if (running && !choice) {
            try {
              const snapshot = await api<Stats>('/api/stats?lite=1')
              const model = snapshot.model || {}
              choice =
                current.configs.find((c) =>
                  c.flags.some(
                    (f) =>
                      (f.flag === '-a' || f.flag === '--alias') &&
                      f.enabled !== false &&
                      f.value.trim() === model.name,
                  ),
                )?.name ||
                current.configs.find(
                  (c) =>
                    model.path &&
                    basename(c.model_path).toLowerCase() ===
                      basename(model.path).toLowerCase(),
                )?.name ||
                ''
            } catch {
              /* blank form remains available */
            }
          }
          if (!live) return
          loadConfig(choice, current.configs)
          if (choice)
            notify(
              running
                ? `Loaded “${choice}” for the running server.`
                : `Loaded default configuration “${choice}”.`,
            )
        }
      } catch {
        if (live) notify('Could not reach launcher settings.', true)
      } finally {
        pending = false
      }
    }
    begin()
    loadFlags()
    const timer = setInterval(begin, 3000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [loadFlags])

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const defaultName = () =>
    draft.flags
      .find(
        (f) => (f.flag === '-a' || f.flag === '--alias') && f.enabled !== false,
      )
      ?.value.trim() || basename(draft.model_path).replace(/\.gguf$/i, '')
  const persist = async (cfg: Config) => {
    const saved = {
      ...cfg,
      name: cfg.name.trim(),
      model_path: cfg.model_path.trim(),
      flags: cfg.flags.filter((f) => f.flag.trim()).map(cleanFlag),
    }
    const response = await post<{ configs: Config[] }>('/api/configs', saved)
    if (stateRef.current)
      applyState({ ...stateRef.current, configs: response.configs })
    setLoaded(structuredClone(saved))
    setDraft(structuredClone(saved))
    notify(`Saved “${saved.name}”.`)
    return true
  }
  const save = async () => {
    const name = draft.name.trim() || defaultName()
    if (!name) {
      notify('Enter a configuration name first.', true)
      return false
    }
    try {
      return await persist({ ...draft, name })
    } catch (error) {
      notify(`Save failed: ${(error as Error).message}`, true)
      return false
    }
  }
  const saveAs = async (name: string) => {
    if (!name.trim()) return false
    try {
      const ok = await persist({ ...draft, name: name.trim() })
      setModal(null)
      if (switchAfterSaveAs !== null) {
        loadConfig(switchAfterSaveAs)
        setSwitchAfterSaveAs(null)
      }
      return ok
    } catch (error) {
      notify(`Save failed: ${(error as Error).message}`, true)
      return false
    }
  }
  const selectConfig = (name: string) => {
    if (name === (loaded?.name || '')) return
    if (!dirty) {
      loadConfig(name)
      return
    }
    setTargetConfig(name)
    setModal('switch')
  }
  const launchAction = async (action: 'launch' | 'stop' | 'restart') => {
    const notify = (text: string, bad = false) =>
      setMessage({ text, bad, target: 'launch' })
    if (action === 'launch' && !draft.model_path.trim()) {
      notify('Select a model (.gguf) first.', true)
      return
    }
    if (
      action === 'launch' &&
      (!draft.port || draft.port < 1 || draft.port > 65535)
    ) {
      notify('Enter a valid port.', true)
      return
    }
    notify(
      action === 'launch'
        ? 'Launching…'
        : action === 'restart'
          ? 'Restarting…'
          : 'Stopping…',
    )
    try {
      const next = await post<LauncherState>(
        `/api/launcher/${action}`,
        action === 'launch'
          ? {
              ...draft,
              flags: draft.flags.filter((f) => f.flag.trim()).map(cleanFlag),
            }
          : {},
      )
      applyState(next)
      notify(
        action === 'launch'
          ? 'Launched. Monitoring the new server.'
          : action === 'restart'
            ? 'Restarted.'
            : 'Stopped.',
      )
    } catch (error) {
      notify(
        `${action[0].toUpperCase() + action.slice(1)} failed: ${(error as Error).message}`,
        true,
      )
    }
  }
  const toggleDefault = async () => {
    if (!loaded || !state) return
    const name =
      state.settings.default_config === loaded.name ? '' : loaded.name
    try {
      applyState(await post<LauncherState>('/api/configs/default', { name }))
      notify(name ? `“${name}” is now the default.` : 'Default cleared.')
    } catch (error) {
      notify(`Could not set default: ${(error as Error).message}`, true)
    }
  }
  const saveAutostart = async (enabled: boolean) => {
    setSavingAutostart(true)
    setAutostartSaveError('')
    try {
      applyState(
        await post<LauncherState>('/api/launcher/settings', {
          autostart_default_model: enabled,
        }),
      )
    } catch (error) {
      setAutostartSaveError(
        `Could not save setting: ${(error as Error).message}`,
      )
    } finally {
      setSavingAutostart(false)
    }
  }
  const deleteConfig = async () => {
    if (!loaded) return
    try {
      const name = loaded.name
      const result = await api<{ configs: Config[] }>(
        `/api/configs/${encodeURIComponent(name)}`,
        { method: 'DELETE' },
      )
      if (stateRef.current)
        applyState({
          ...stateRef.current,
          configs: result.configs,
          settings: {
            ...stateRef.current.settings,
            default_config:
              stateRef.current.settings.default_config === name
                ? null
                : stateRef.current.settings.default_config,
          },
        })
      loadConfig('', result.configs)
      setModal(null)
      notify(`Deleted “${name}”.`)
    } catch (error) {
      notify(`Delete failed: ${(error as Error).message}`, true)
    }
  }

  const flagIndex = useMemo(
    () =>
      new Map(
        knownFlags.flatMap((item) =>
          item.flags.map((alias) => [alias, item] as const),
        ),
      ),
    [knownFlags],
  )
  const filteredFlags = useMemo(() => {
    const query = flagSearch.trim().toLowerCase()
    const score = (item: KnownFlag) => {
      const aliases = item.flags.map((a) => a.toLowerCase())
      return aliases.some((a) => a.replace(/^-+/, '').startsWith(query))
        ? 0
        : aliases.some((a) => a.includes(query))
          ? 1
          : (item.desc || '').toLowerCase().includes(query)
            ? 2
            : 3
    }
    return knownFlags
      .map((item) => ({ item, rank: query ? score(item) : 0 }))
      .filter((x) => x.rank < 3)
      .sort(
        (a, b) =>
          a.rank - b.rank || a.item.flags[0].localeCompare(b.item.flags[0]),
      )
      .slice(0, 50)
      .map((x) => x.item)
  }, [knownFlags, flagSearch])
  const addFlag = (chosen?: string) => {
    const raw = (chosen || flagSearch).trim()
    if (!raw) return
    const flag = flagIndex.has(raw)
      ? raw
      : flagIndex.has(`--${raw}`)
        ? `--${raw}`
        : flagIndex.has(`-${raw}`)
          ? `-${raw}`
          : raw
    update({ flags: [...draft.flags, { flag, value: '' }] })
    setFlagSearch('')
    setFlagOpen(false)
    setFlagSelection(0)
  }
  const editFlag = (index: number, change: Partial<Flag>) =>
    update({
      flags: draft.flags.map((flag, i) =>
        i === index ? { ...flag, ...change } : flag,
      ),
    })

  const goBrowse = async (path: string, type: 'model' | 'binary') => {
    setBrowseLoading(true)
    try {
      setBrowse(
        await api<Browse>(
          `/api/browse?path=${encodeURIComponent(path)}${type === 'model' ? '&ext=.gguf' : navigator.platform.startsWith('Win') ? '&ext=.exe' : ''}`,
        ),
      )
    } catch (error) {
      setBrowse((previous) => ({
        path: previous?.path || path,
        parent: previous?.parent || null,
        dirs: [],
        files: [],
        error: (error as Error).message,
      }))
    } finally {
      setBrowseLoading(false)
    }
  }
  const openBrowse = (type: 'model' | 'binary') => {
    const chosen =
      type === 'model'
        ? draft.model_path || state?.settings.models_dir || ''
        : state?.settings.llama_server_path || ''
    const start = chosen ? chosen.replace(/[\\/][^\\/]*$/, '') : ''
    setBrowse(null)
    setModal(type === 'model' ? 'browse-model' : 'browse-binary')
    goBrowse(start, type)
  }
  const chooseFile = async (path: string) => {
    const notify = (text: string, bad = false) =>
      setMessage({ text, bad, target: 'binary' })
    if (modal === 'browse-model') {
      update({
        model_path: path,
        name: draft.name || basename(path).replace(/\.gguf$/i, ''),
      })
      post('/api/launcher/settings', {
        models_dir: path.replace(/[\\/][^\\/]*$/, ''),
      }).catch(() => {})
    } else if (modal === 'browse-binary') {
      try {
        applyState(
          await post<LauncherState>('/api/launcher/settings', {
            llama_server_path: path,
          }),
        )
        loadFlags()
        notify('llama-server path updated.')
      } catch (error) {
        notify(`Could not set path: ${(error as Error).message}`, true)
      }
    }
    setModal(null)
  }

  useEffect(() => {
    if (modal !== 'console') return
    let live = true
    let pending = false
    consoleOffsetRef.current = 0
    consoleCursorRef.current = ''
    consoleSourceRef.current = ''
    consoleHasRef.current = false
    consoleStickRef.current = true
    setConsoleText('')
    setConsoleOffset(0)
    setConsoleLogError('')
    void loadConsoleRuns()
    const poll = async () => {
      if (pending) return
      pending = true
      try {
        const result = await api<{
          available: boolean
          content: string
          offset: number
          path: string
          source?: string
          cursor?: string
          reset?: boolean
          gap?: boolean
          error?: string | null
        }>(
          `/api/launcher/console?offset=${consoleOffsetRef.current}&source=${consoleSourceRef.current}&run_id=${encodeURIComponent(consoleRun)}&cursor=${encodeURIComponent(consoleCursorRef.current)}`,
        )
        if (!live) return
        setConsolePath(result.path || '')
        setConsoleLogError(
          result.error ||
            (result.gap
              ? 'Older diagnostic output is no longer retained.'
              : ''),
        )
        if (result.content) {
          const clean = result.content.replace(/\x1b\[[0-9;]*m/g, '')
          const replace =
            !consoleHasRef.current ||
            result.reset ||
            result.offset < consoleOffsetRef.current ||
            (result.source || '') !== consoleSourceRef.current
          setConsoleText((previous) => {
            const text = replace ? clean : previous + clean
            return text.length > 512 * 1024
              ? '[Earlier output omitted from display]\n' +
                  text.slice(-(512 * 1024))
              : text
          })
          consoleHasRef.current = true
        } else if (!consoleHasRef.current)
          setConsoleText(
            result.available
              ? '(log is empty)'
              : '(no log yet — launch a server from Monitor)',
          )
        consoleOffsetRef.current = result.offset
        consoleCursorRef.current = result.cursor || ''
        consoleSourceRef.current = result.source || ''
        setConsoleOffset(result.offset)
        requestAnimationFrame(() => {
          if (consoleStickRef.current && consolePreRef.current)
            consolePreRef.current.scrollTop = consolePreRef.current.scrollHeight
        })
      } catch {
        if (live && !consoleHasRef.current)
          setConsoleText('Could not read console output.')
      } finally {
        pending = false
      }
    }
    poll()
    const timer = setInterval(poll, 1000)
    const runsTimer = setInterval(() => void loadConsoleRuns('', true), 10000)
    return () => {
      live = false
      clearInterval(timer)
      clearInterval(runsTimer)
    }
  }, [modal, consoleRun, loadConsoleRuns])

  const status = state?.status.state || 'stopped'
  const gamingBlocked = !!state?.gaming?.blocked
  const pendingPrimary = state?.gaming?.servers.some(
    (server) => server.id === 'primary' && server.status !== 'cancelled',
  )
  const statusText =
    status === 'running'
      ? `Running${state?.status.config_name ? ` · ${state.status.config_name}` : ''}`
      : status === 'exited'
        ? `Exited (code ${state?.status.exit_code})`
        : 'Stopped'
  return {
    launchAction,
    diagnostics,
    diagnosticsError,
    diagnosing,
    runDiagnostics,
    state,
    draft,
    loaded,
    dirty,
    changed,
    message,
    status,
    statusText,
    gamingBlocked,
    pendingPrimary,
    modal,
    setModal,
    update,
    selectConfig,
    toggleDefault,
    saveAutostart,
    savingAutostart,
    autostartSaveError,
    deleteConfig,
    save,
    saveAs,
    defaultName,
    newName,
    setNewName,
    targetConfig,
    loadConfig,
    setSwitchAfterSaveAs,
    flagsCollapsed,
    setFlagsCollapsed,
    flagSearch,
    setFlagSearch,
    flagOpen,
    setFlagOpen,
    flagSelection,
    setFlagSelection,
    flagIndex,
    filteredFlags,
    flagSource,
    addFlag,
    editFlag,
    browse,
    browseLoading,
    goBrowse,
    openBrowse,
    chooseFile,
    consolePath,
    consoleRuns,
    consoleRun,
    setConsoleRun,
    consoleRunCursor,
    loadConsoleRuns,
    consoleLogError,
    consoleOffset,
    consoleText,
    consolePreRef,
    consoleStickRef,
    deleteButtonRef,
  }
}

export type LauncherController = ReturnType<typeof useLauncher>
