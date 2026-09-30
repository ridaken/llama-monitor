import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  File,
  Folder,
  FolderOpen,
  Play,
  RotateCcw,
  Save,
  Search,
  Square,
  Star,
  Terminal,
  Trash2,
  X,
} from 'lucide-react'
import { api, post } from '@/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { basename } from '@/lib/utils'
import type {
  Config,
  Flag,
  FlagResponse,
  KnownFlag,
  LauncherState,
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
const initialCollapsed = () => {
  try {
    return localStorage.getItem('lx-flags-collapsed') === '1'
  } catch {
    return false
  }
}

export function Manage() {
  const [state, setState] = useState<LauncherState | null>(null)
  const [knownFlags, setKnownFlags] = useState<KnownFlag[]>([])
  const [flagSource, setFlagSource] = useState('bundled')
  const [loaded, setLoaded] = useState<Config | null>(null)
  const [draft, setDraft] = useState<Config>(blank(8001))
  const [message, setMessage] = useState<{ text: string; bad?: boolean }>({
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
  const [flagsCollapsed, setFlagsCollapsed] = useState(initialCollapsed)
  const [browse, setBrowse] = useState<Browse | null>(null)
  const [browseLoading, setBrowseLoading] = useState(false)
  const [consoleText, setConsoleText] = useState('')
  const [consolePath, setConsolePath] = useState('')
  const [consoleOffset, setConsoleOffset] = useState(0)
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
    try {
      localStorage.setItem('lx-flags-collapsed', flagsCollapsed ? '1' : '0')
    } catch {
      /* optional preference */
    }
  }, [flagsCollapsed])
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
    consoleHasRef.current = false
    consoleStickRef.current = true
    setConsoleText('')
    setConsoleOffset(0)
    const poll = async () => {
      if (pending) return
      pending = true
      try {
        const result = await api<{
          available: boolean
          content: string
          offset: number
          path: string
        }>(`/api/launcher/console?offset=${consoleOffsetRef.current}`)
        if (!live) return
        setConsolePath(result.path || '')
        if (result.content) {
          const clean = result.content.replace(/\x1b\[[0-9;]*m/g, '')
          setConsoleText((previous) =>
            !consoleHasRef.current || result.offset < consoleOffsetRef.current
              ? clean
              : previous + clean,
          )
          consoleHasRef.current = true
        } else if (!consoleHasRef.current)
          setConsoleText(
            result.available
              ? '(log is empty)'
              : '(no log yet — launch a server from Manage)',
          )
        consoleOffsetRef.current = result.offset
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
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [modal])

  const status = state?.status.state || 'stopped'
  const statusText =
    status === 'running'
      ? `Running${state?.status.config_name ? ` · ${state.status.config_name}` : ''}`
      : status === 'exited'
        ? `Exited (code ${state?.status.exit_code})`
        : 'Stopped'
  return (
    <div className="space-y-5">
      <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
        <Card className="surface rounded-2xl">
          <CardHeader>
            <CardTitle className="flex items-center gap-3 text-xl">
              Launch & manage{' '}
              <Badge
                variant={status === 'stopped' ? 'secondary' : 'default'}
                className={
                  status === 'running'
                    ? 'bg-teal-700'
                    : status === 'exited'
                      ? 'bg-amber-700'
                      : ''
                }
              >
                {statusText}
              </Badge>
            </CardTitle>
            <CardDescription>
              Configure a model, then launch or manage llama-server here.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            <Button
              id="lx-launch"
              disabled={!state?.binary_valid}
              onClick={() => launchAction('launch')}
            >
              <Play />
              Launch
            </Button>
            <Button
              id="lx-stop"
              variant="secondary"
              disabled={status !== 'running'}
              onClick={() => launchAction('stop')}
            >
              <Square />
              Stop
            </Button>
            <Button
              id="lx-restart"
              variant="secondary"
              disabled={!state?.status.config_name}
              onClick={() => launchAction('restart')}
            >
              <RotateCcw />
              Restart
            </Button>
            <Button
              id="lx-console"
              variant="outline"
              onClick={() => setModal('console')}
            >
              <Terminal />
              Console
            </Button>
          </CardContent>
        </Card>
        <Card className="surface rounded-2xl">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">llama-server binary</CardTitle>
          </CardHeader>
          <CardContent>
            <p
              id="lx-bin-path"
              className="mono mb-4 break-all text-xs text-muted-foreground"
            >
              {state?.settings.llama_server_path || 'Not set'}
            </p>
            {!state?.binary_valid && (
              <p id="lx-bin-msg" className="mb-3 text-sm text-amber-300">
                Set a valid binary before launching.
              </p>
            )}
            <Button
              id="lx-bin-browse"
              variant="outline"
              size="sm"
              onClick={() => openBrowse('binary')}
            >
              <FolderOpen />
              Browse executable
            </Button>
          </CardContent>
        </Card>
      </div>
      <Card className="surface rounded-2xl">
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-3 text-lg">
            Configuration{' '}
            {dirty && (
              <Badge
                id="lx-dirty-mark"
                variant="outline"
                className="border-amber-500 text-amber-300"
              >
                Unsaved changes
              </Badge>
            )}
          </CardTitle>
          <CardDescription>
            Saved configurations persist across dashboard restarts.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-56 flex-1 space-y-2">
              <Label htmlFor="lx-config">Saved configuration</Label>
              <Select
                value={loaded?.name || 'new'}
                onValueChange={(value) =>
                  selectConfig(value === 'new' ? '' : value)
                }
              >
                <SelectTrigger id="lx-config" className="w-full">
                  <SelectValue placeholder="New configuration" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="new">New configuration</SelectItem>
                  {state?.configs.map((cfg) => (
                    <SelectItem key={cfg.name} value={cfg.name}>
                      {cfg.name === state.settings.default_config ? '★ ' : ''}
                      {cfg.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button
              id="lx-default"
              variant="outline"
              disabled={!loaded}
              onClick={toggleDefault}
              aria-label={
                loaded && state?.settings.default_config === loaded.name
                  ? 'Clear default configuration'
                  : 'Set as default configuration'
              }
            >
              <Star
                className={
                  loaded && state?.settings.default_config === loaded.name
                    ? 'fill-amber-300 text-amber-300'
                    : ''
                }
              />
              {loaded && state?.settings.default_config === loaded.name
                ? 'Default'
                : 'Set default'}
            </Button>
            <Button
              id="lx-delete"
              ref={deleteButtonRef}
              variant="outline"
              disabled={!loaded}
              onClick={() => setModal('delete')}
            >
              <Trash2 />
              Delete
            </Button>
          </div>
          <div className="grid gap-4 md:grid-cols-[1fr_180px]">
            <div className="space-y-2">
              <Label htmlFor="lx-model">Model (.gguf)</Label>
              <div className="flex gap-2">
                <Input
                  id="lx-model"
                  value={draft.model_path}
                  onChange={(e) => update({ model_path: e.target.value })}
                  placeholder="Path to model.gguf"
                  className={`mono ${changed.model ? 'border-amber-500' : ''}`}
                />
                <Button
                  id="lx-model-browse"
                  variant="outline"
                  onClick={() => openBrowse('model')}
                  aria-label="Browse model"
                >
                  <FolderOpen />
                </Button>
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="lx-port">Port</Label>
              <Input
                id="lx-port"
                type="number"
                min={1}
                max={65535}
                value={draft.port ?? ''}
                className={changed.port ? 'border-amber-500' : ''}
                onChange={(e) =>
                  update({
                    port: e.target.value === '' ? null : Number(e.target.value),
                  })
                }
              />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Monitoring automatically adds --port, --metrics, and --log-file. The
            port is required.
          </p>
          <div
            className={`rounded-xl border bg-background/20 p-4 ${changed.prompts ? 'border-amber-500' : 'border-border/70'}`}
          >
            <Label htmlFor="lx-log-prompts" className="flex items-start gap-3">
              <Checkbox
                id="lx-log-prompts"
                checked={draft.log_prompts}
                onCheckedChange={(value) =>
                  update({ log_prompts: value === true })
                }
              />
              <span>
                <strong className="block text-sm">
                  Save prompts in Activity history
                </strong>
                <span className="mt-1 block text-xs font-normal text-muted-foreground">
                  Uses supported llama-server debugging files. Prompt text may
                  contain private data; responses are not saved. Relaunch to
                  apply.
                </span>
              </span>
            </Label>
          </div>
          <div className="space-y-3">
            <Button
              id="lx-flags-label"
              variant="ghost"
              className={`px-0 text-base font-semibold ${changed.flags ? 'text-amber-300' : ''}`}
              onClick={() => setFlagsCollapsed(!flagsCollapsed)}
              aria-expanded={!flagsCollapsed}
              aria-controls="lx-flags-editor"
            >
              {flagsCollapsed ? <ChevronRight /> : <ChevronDown />}Flags{' '}
              <span className="text-xs font-normal text-muted-foreground">
                ({draft.flags.length})
              </span>
            </Button>
            {flagsCollapsed ? (
              <button
                id="lx-flags-summary"
                type="button"
                className="focus-ring flex w-full flex-wrap items-center gap-2 rounded-xl border border-dashed border-border p-3 text-left"
                onClick={() => setFlagsCollapsed(false)}
              >
                {draft.flags.length ? (
                  draft.flags.map((flag, i) => (
                    <Badge
                      key={i}
                      variant="secondary"
                      className={
                        flag.enabled === false ? 'opacity-50 line-through' : ''
                      }
                    >
                      {flag.flag} {flag.value}
                    </Badge>
                  ))
                ) : (
                  <span className="text-sm text-muted-foreground">
                    No flags set
                  </span>
                )}
                <span className="ml-auto text-xs text-primary">
                  Expand to edit
                </span>
              </button>
            ) : (
              <div id="lx-flags-editor" className="space-y-3">
                {draft.flags.map((flag, index) => {
                  const info = flagIndex.get(flag.flag.trim())
                  return (
                    <div
                      key={index}
                      className="lx-flag-row rounded-xl border border-border/70 bg-background/20 p-3"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Checkbox
                          className="lx-en"
                          checked={flag.enabled !== false}
                          onCheckedChange={(value) =>
                            editFlag(index, { enabled: value === true })
                          }
                          aria-label={`Enable ${flag.flag || `flag ${index + 1}`}`}
                        />
                        <Input
                          className="lx-flag mono w-36"
                          value={flag.flag}
                          onChange={(e) =>
                            editFlag(index, { flag: e.target.value })
                          }
                          aria-label={`Flag ${index + 1}`}
                        />
                        <Input
                          className="lx-val mono min-w-36 flex-1"
                          value={flag.value}
                          onChange={(e) =>
                            editFlag(index, { value: e.target.value })
                          }
                          placeholder={info?.value_hint || 'Value'}
                          aria-label={`Value for ${flag.flag || `flag ${index + 1}`}`}
                        />
                        <Button
                          className="x"
                          variant="ghost"
                          size="icon"
                          onClick={() =>
                            update({
                              flags: draft.flags.filter((_, i) => i !== index),
                            })
                          }
                          aria-label={`Remove ${flag.flag || `flag ${index + 1}`}`}
                        >
                          <X />
                        </Button>
                      </div>
                      {info?.desc && (
                        <p className="mt-2 pl-7 text-xs text-muted-foreground">
                          {info.desc}
                        </p>
                      )}
                    </div>
                  )
                })}
                <div className="relative flex gap-2">
                  <div className="relative min-w-0 flex-1">
                    <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" />
                    <Input
                      id="lx-flag-pick"
                      className="pl-9"
                      value={flagSearch}
                      onChange={(e) => {
                        setFlagSearch(e.target.value)
                        setFlagOpen(true)
                        setFlagSelection(0)
                      }}
                      onFocus={() => setFlagOpen(true)}
                      onBlur={() => setFlagOpen(false)}
                      onKeyDown={(e) => {
                        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                          e.preventDefault()
                          setFlagOpen(true)
                          if (filteredFlags.length)
                            setFlagSelection(
                              (index) =>
                                (index +
                                  (e.key === 'ArrowDown' ? 1 : -1) +
                                  filteredFlags.length) %
                                filteredFlags.length,
                            )
                        }
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          addFlag(
                            flagOpen
                              ? filteredFlags[flagSelection]?.flags[0]
                              : undefined,
                          )
                        }
                        if (e.key === 'Escape') setFlagOpen(false)
                      }}
                      placeholder="Search supported flags or enter a custom flag"
                      role="combobox"
                      aria-expanded={flagOpen}
                      aria-controls="lx-flag-list"
                      aria-activedescendant={
                        flagOpen && filteredFlags.length
                          ? `lx-flag-option-${flagSelection}`
                          : undefined
                      }
                    />
                    {flagOpen && (
                      <div
                        id="lx-flag-list"
                        role="listbox"
                        className="absolute z-30 mt-1 max-h-56 w-full overflow-auto rounded-lg border border-border bg-popover p-1 shadow-xl"
                      >
                        {filteredFlags.length ? (
                          filteredFlags.map((item, index) => (
                            <button
                              key={item.flags.join(',')}
                              id={`lx-flag-option-${index}`}
                              type="button"
                              role="option"
                              aria-selected={index === flagSelection}
                              className={`focus-ring block w-full rounded px-3 py-2 text-left hover:bg-accent ${index === flagSelection ? 'bg-accent' : ''}`}
                              onMouseDown={(e) => e.preventDefault()}
                              onClick={() => addFlag(item.flags[0])}
                            >
                              <span className="mono text-xs text-primary">
                                {item.flags.join(', ')} {item.value_hint || ''}
                              </span>
                              <span className="block text-xs text-muted-foreground">
                                {item.desc}
                              </span>
                            </button>
                          ))
                        ) : (
                          <div className="p-3 text-xs text-muted-foreground">
                            No match. Add your entry as a custom flag.
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                  <Button
                    id="lx-flag-add"
                    variant="outline"
                    onClick={() => addFlag()}
                  >
                    Add flag
                  </Button>
                </div>
                {flagSource !== 'help' && (
                  <p id="lx-flags-src" className="text-xs text-amber-300">
                    Bundled flag list. Set a valid binary to load every
                    supported flag.
                  </p>
                )}
              </div>
            )}
          </div>
          <div className="grid gap-4 md:grid-cols-[1fr_auto]">
            <div className="space-y-2">
              <Label htmlFor="lx-name">Configuration name</Label>
              <Input
                id="lx-name"
                value={draft.name}
                className={changed.name ? 'border-amber-500' : ''}
                onChange={(e) => update({ name: e.target.value })}
                placeholder="Name this configuration"
              />
            </div>
            <div className="flex items-end gap-2">
              <Button id="lx-save" onClick={save}>
                <Save />
                Save
              </Button>
              <Button
                id="lx-saveas"
                variant="outline"
                onClick={() => {
                  setSwitchAfterSaveAs(null)
                  setNewName(defaultName())
                  setModal('save-as')
                }}
              >
                Save as new
              </Button>
            </div>
          </div>
          <p
            id="lx-msg"
            role="status"
            className={`min-h-5 text-sm ${message.bad ? 'text-red-300' : 'text-teal-300'}`}
          >
            {message.text}
          </p>
        </CardContent>
      </Card>
      <Dialog
        open={modal === 'save-as'}
        onOpenChange={(open) => !open && setModal(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Save as new configuration</DialogTitle>
            <DialogDescription>
              Choose a name for this model and its flags.
            </DialogDescription>
          </DialogHeader>
          <Input
            id="lx-name-input"
            autoFocus
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveAs(newName)
            }}
            aria-label="New configuration name"
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setModal(null)}>
              Cancel
            </Button>
            <Button disabled={!newName.trim()} onClick={() => saveAs(newName)}>
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={modal === 'switch'}
        onOpenChange={(open) => !open && setModal(null)}
      >
        <DialogContent id="lx-modal">
          <DialogHeader>
            <DialogTitle>Unsaved changes</DialogTitle>
            <DialogDescription>
              Save or discard your changes before switching configurations.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button variant="outline" onClick={() => setModal(null)}>
              Cancel
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                setModal(null)
                loadConfig(targetConfig)
              }}
            >
              Discard changes
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                setSwitchAfterSaveAs(targetConfig)
                setNewName(defaultName())
                setModal('save-as')
              }}
            >
              Save as new
            </Button>
            <Button
              onClick={async () => {
                if (await save()) {
                  setModal(null)
                  loadConfig(targetConfig)
                }
              }}
            >
              Save and switch
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={modal === 'delete'}
        onOpenChange={(open) => !open && setModal(null)}
      >
        <DialogContent
          onCloseAutoFocus={(event) => {
            event.preventDefault()
            deleteButtonRef.current?.focus()
          }}
        >
          <DialogHeader>
            <DialogTitle>Delete configuration?</DialogTitle>
            <DialogDescription>
              Delete the saved configuration “{loaded?.name}”? The running
              server is unaffected.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setModal(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={deleteConfig}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={modal === 'browse-model' || modal === 'browse-binary'}
        onOpenChange={(open) => !open && setModal(null)}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {modal === 'browse-model'
                ? 'Select a model (.gguf)'
                : 'Select llama-server executable'}
            </DialogTitle>
            <DialogDescription className="mono break-all">
              {browse?.path || 'This PC'}
            </DialogDescription>
          </DialogHeader>
          <div
            id="lx-browse-list"
            className="max-h-[55vh] overflow-auto rounded-xl border border-border"
          >
            {browseLoading && (
              <p className="p-4 text-sm text-muted-foreground">
                Loading files…
              </p>
            )}
            {browse?.error && (
              <p className="p-4 text-sm text-red-300">
                Cannot open: {browse.error}
              </p>
            )}
            {browse?.parent != null && (
              <button
                className="focus-ring flex w-full items-center gap-3 border-b border-border/50 p-3 text-left hover:bg-accent"
                onClick={() =>
                  goBrowse(
                    browse.parent || '',
                    modal === 'browse-model' ? 'model' : 'binary',
                  )
                }
              >
                <Folder />
                ..
              </button>
            )}
            {browse?.dirs.map((dir) => (
              <button
                key={dir}
                className="focus-ring flex w-full items-center gap-3 border-b border-border/50 p-3 text-left hover:bg-accent"
                onClick={() =>
                  goBrowse(dir, modal === 'browse-model' ? 'model' : 'binary')
                }
              >
                <Folder className="size-4 text-primary" />
                {basename(dir) || dir}
              </button>
            ))}
            {browse?.files.map((file) => (
              <button
                key={file}
                className="focus-ring flex w-full items-center gap-3 border-b border-border/50 p-3 text-left hover:bg-accent"
                onClick={() => chooseFile(file)}
              >
                <File className="size-4 text-teal-300" />
                {basename(file)}
              </button>
            ))}
            {browse &&
              !browseLoading &&
              !browse.error &&
              !browse.dirs.length &&
              !browse.files.length && (
                <p className="p-4 text-sm text-muted-foreground">
                  No matching files here.
                </p>
              )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setModal(null)}>
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={modal === 'console'}
        onOpenChange={(open) => !open && setModal(null)}
      >
        <DialogContent className="flex h-[80vh] max-w-5xl flex-col">
          <DialogHeader>
            <DialogTitle>llama-server console</DialogTitle>
            <DialogDescription className="mono break-all">
              {consolePath} · offset {consoleOffset}
            </DialogDescription>
          </DialogHeader>
          <pre
            id="console-out"
            ref={consolePreRef}
            onScroll={(e) => {
              const el = e.currentTarget
              consoleStickRef.current =
                el.scrollHeight - el.scrollTop - el.clientHeight < 24
            }}
            className="mono min-h-0 flex-1 overflow-auto rounded-xl border border-border bg-background p-4 text-xs leading-relaxed whitespace-pre-wrap break-words"
          >
            {consoleText}
          </pre>
          <DialogFooter>
            <Button
              id="console-close"
              variant="outline"
              onClick={() => setModal(null)}
            >
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
