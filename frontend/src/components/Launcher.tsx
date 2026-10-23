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
  Stethoscope,
  Terminal,
  Trash2,
  X,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
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
import type { LauncherController } from '@/hooks/useLauncher'

export function LauncherFeedback({
  launcher,
  target,
}: {
  launcher: LauncherController
  target: 'configuration' | 'launch' | 'binary'
}) {
  const { message } = launcher
  if (!message.text || (message.target || 'configuration') !== target)
    return null
  return (
    <p
      id={
        target === 'configuration'
          ? 'lx-msg'
          : target === 'launch'
            ? 'lx-launch-msg'
            : 'lx-bin-status'
      }
      role="status"
      className={`launcher-feedback text-xs ${message.bad ? 'text-red-300' : 'text-teal-300'}`}
    >
      {message.text}
    </p>
  )
}

export function LaunchStatus({ launcher }: { launcher: LauncherController }) {
  const { status, statusText } = launcher
  return (
    <Badge
      id="lx-status"
      variant="outline"
      className={`max-w-full break-all whitespace-normal text-xs ${status === 'running' ? 'border-teal-700/60 bg-teal-950/30 text-teal-300' : status === 'exited' ? 'border-amber-700 text-amber-300' : 'text-muted-foreground'}`}
    >
      {statusText}
    </Badge>
  )
}

export function LaunchControls({ launcher }: { launcher: LauncherController }) {
  const {
    state,
    gamingBlocked,
    pendingPrimary,
    status,
    launchAction,
    setModal,
  } = launcher
  return (
    <div className="launch-controls space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button
          id="lx-launch"
          disabled={!state?.binary_valid || gamingBlocked}
          onClick={() => launchAction('launch')}
        >
          <Play />
          Launch
        </Button>
        <Button
          id="lx-stop"
          variant="secondary"
          disabled={status !== 'running' && !pendingPrimary}
          onClick={() => launchAction('stop')}
        >
          <Square />
          Stop
        </Button>
        <Button
          id="lx-restart"
          variant="secondary"
          disabled={!state?.status.config_name || gamingBlocked}
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
      </div>
      {state && !state.binary_valid && (
        <p className="text-xs text-amber-300">
          Set a valid executable in{' '}
          <a
            href="#manage"
            className="focus-ring rounded text-primary underline underline-offset-2"
          >
            Manage
          </a>{' '}
          before launching.
        </p>
      )}
      <LauncherFeedback launcher={launcher} target="launch" />
      {status === 'exited' && (
        <p className="text-xs text-amber-300" role="status">
          llama-server exited with code {state?.status.exit_code}. Open Console
          for startup output. A crash code alone does not identify its cause.
        </p>
      )}
      {launcher.diagnosticsError && (
        <p role="alert" className="text-xs text-red-300">
          {launcher.diagnosticsError}
        </p>
      )}
      {launcher.diagnosing && (
        <p role="status" className="text-xs">
          Checking startup diagnostics…
        </p>
      )}
      {launcher.diagnostics && (
        <div
          id="lx-diagnostics"
          className="space-y-2 rounded-lg border p-3 text-xs"
        >
          <p role="status">{launcher.diagnostics.summary}</p>
          {launcher.diagnostics.checks.map((check) => (
            <details key={check.name} open={check.state === 'fail'}>
              <summary className="cursor-pointer">
                {check.name === 'nvidia'
                  ? 'NVIDIA access'
                  : 'Server device initialization'}
                : {check.state} — {check.message}
              </summary>
              {check.output && (
                <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all">
                  {check.output}
                </pre>
              )}
              {check.truncated && <p>Showing the last 16 KiB of output.</p>}
            </details>
          ))}
          {launcher.diagnostics.recovery.length > 0 && (
            <div className="space-y-2 text-amber-300">
              <p className="font-medium">Recovery guidance</p>
              {launcher.diagnostics.recovery.map((step) => (
                <p key={step}>{step}</p>
              ))}
            </div>
          )}
        </div>
      )}
      {(launcher.diagnostics || launcher.diagnosticsError) && (
        <Button
          id="lx-doctor-retry"
          variant="outline"
          onClick={launcher.runDiagnostics}
        >
          <Stethoscope /> Run diagnostics again
        </Button>
      )}
    </div>
  )
}

export function ConfigurationPanel({
  launcher,
}: {
  launcher: LauncherController
}) {
  const {
    state,
    draft,
    loaded,
    dirty,
    changed,
    update,
    selectConfig,
    toggleDefault,
    deleteButtonRef,
    setModal,
    flagsCollapsed,
    setFlagsCollapsed,
    flagIndex,
    editFlag,
    flagSearch,
    setFlagSearch,
    flagOpen,
    setFlagOpen,
    flagSelection,
    setFlagSelection,
    filteredFlags,
    addFlag,
    flagSource,
    save,
    setSwitchAfterSaveAs,
    setNewName,
    defaultName,
    openBrowse,
  } = launcher
  return (
    <Card
      id="configuration-panel"
      className="surface configuration-panel rounded-2xl"
    >
      <CardHeader className="configuration-header">
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
      </CardHeader>
      <CardContent className="configuration-content space-y-4">
        <div className="configuration-picker flex items-end gap-2">
          <div className="min-w-0 flex-1 space-y-1">
            <Label htmlFor="lx-config">Saved configuration</Label>
            <Select
              value={loaded?.name || 'new'}
              onValueChange={(value) =>
                selectConfig(value === 'new' ? '' : value)
              }
            >
              <SelectTrigger
                id="lx-config"
                className="w-full"
                title={loaded?.name || 'New configuration'}
              >
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
        <div className="configuration-model grid grid-cols-[minmax(0,1fr)_76px] gap-3">
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
              readOnly={!!state?.runtime?.containerized}
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
          {state?.runtime?.containerized
            ? 'Docker uses internal port 8001. Change INFERENCE_PORT in .env for a different host port. Models are mounted at /models.'
            : 'Port, metrics, and log file are set automatically.'}
        </p>
        <div
          className={`configuration-prompts rounded-xl border bg-background/20 p-3 ${changed.prompts ? 'border-amber-500' : 'border-border/70'}`}
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
                Prompts may contain private data; responses are not saved. Save
                and relaunch to apply.
              </span>
            </span>
          </Label>
        </div>
        <div className="configuration-flags space-y-1">
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
              className="flags-summary focus-ring flex w-full items-center gap-2 rounded-xl border border-dashed border-border px-3 py-2 text-left"
              onClick={() => setFlagsCollapsed(false)}
            >
              {draft.flags.length ? (
                draft.flags.slice(0, 2).map((flag, i) => (
                  <Badge
                    key={i}
                    variant="secondary"
                    className={`max-w-32 truncate ${flag.enabled === false ? 'opacity-50 line-through' : ''}`}
                  >
                    {flag.flag} {flag.value}
                  </Badge>
                ))
              ) : (
                <span className="text-sm text-muted-foreground">
                  No flags set
                </span>
              )}
              {draft.flags.length > 2 && (
                <span className="shrink-0 text-xs text-muted-foreground">
                  +{draft.flags.length - 2}
                </span>
              )}
              <span className="ml-auto shrink-0 text-xs text-primary">
                Edit flags
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
                  Bundled flag list. Set a valid binary to load every supported
                  flag.
                </p>
              )}
            </div>
          )}
        </div>
        <div className="configuration-save grid gap-3">
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
        <LauncherFeedback launcher={launcher} target="configuration" />
      </CardContent>
    </Card>
  )
}

export function LauncherDialogs({
  launcher,
}: {
  launcher: LauncherController
}) {
  const {
    modal,
    setModal,
    newName,
    setNewName,
    saveAs,
    targetConfig,
    loadConfig,
    setSwitchAfterSaveAs,
    defaultName,
    save,
    loaded,
    deleteButtonRef,
    deleteConfig,
    browse,
    browseLoading,
    goBrowse,
    chooseFile,
    consolePath,
    consoleOffset,
    consoleText,
    consolePreRef,
    consoleStickRef,
  } = launcher
  return (
    <>
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
            <Button
              id="lx-doctor"
              variant="outline"
              disabled={launcher.diagnosing}
              onClick={() => {
                setModal(null)
                launcher.runDiagnostics()
              }}
            >
              <Stethoscope /> Run diagnostics
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
