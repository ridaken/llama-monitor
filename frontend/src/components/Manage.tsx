import { FolderOpen } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Gaming } from '@/components/Gaming'
import { Startup } from '@/components/Startup'
import { ProxySettings } from '@/components/ProxySettings'
import { LauncherFeedback } from '@/components/Launcher'
import type { LauncherController } from '@/hooks/useLauncher'

export function Manage({ launcher }: { launcher: LauncherController }) {
  const {
    state,
    openBrowse,
    saveAutostart,
    savingAutostart,
    autostartSaveError,
  } = launcher
  return (
    <div className="manage-layout">
      <Card className="surface rounded-2xl" id="binary-panel">
        <CardHeader className="manage-panel-header pb-2">
          <CardTitle className="text-sm">llama-server binary</CardTitle>
        </CardHeader>
        <CardContent className="manage-panel-content">
          {state?.runtime?.containerized && (
            <p className="mb-3 text-sm text-muted-foreground">
              Docker includes llama-server. Browse models under /models; host
              paths and Windows executables are unavailable inside the
              container.
            </p>
          )}
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
          <LauncherFeedback launcher={launcher} target="binary" />
        </CardContent>
      </Card>

      <Card className="surface rounded-2xl" id="model-startup-panel">
        <CardHeader className="manage-panel-header">
          <CardTitle>Model startup</CardTitle>
        </CardHeader>
        <CardContent className="manage-panel-content space-y-4">
          <div className="flex items-start gap-3">
            <Checkbox
              id="autostart-default-model"
              checked={state?.settings.autostart_default_model === true}
              disabled={!state || savingAutostart}
              onCheckedChange={(checked) => saveAutostart(checked === true)}
              aria-describedby="model-startup-help model-startup-default"
            />
            <Label htmlFor="autostart-default-model" className="leading-5">
              Start the default model when llama-monitor starts
            </Label>
          </div>
          <p id="model-startup-help" className="text-sm text-muted-foreground">
            Applies the next time llama-monitor starts, even without an open
            browser. Existing models, recovery, and active gaming take priority.
          </p>
          <p id="model-startup-default" className="break-words text-sm">
            {state?.settings.default_config
              ? `Default configuration: ${state.settings.default_config}`
              : 'No default configuration. Select a saved configuration in Monitor and click Set default.'}
          </p>
          {autostartSaveError && (
            <p role="alert" className="text-sm text-amber-300">
              {autostartSaveError}
            </p>
          )}
          {state?.autostart_error && (
            <p role="alert" className="text-sm text-amber-300">
              {state.autostart_error}
            </p>
          )}
        </CardContent>
      </Card>

      <ProxySettings />
      <Gaming />
      <Startup />
    </div>
  )
}
