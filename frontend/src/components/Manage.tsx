import { FolderOpen } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Gaming } from '@/components/Gaming'
import { Startup } from '@/components/Startup'
import { LauncherFeedback } from '@/components/Launcher'
import type { LauncherController } from '@/hooks/useLauncher'

export function Manage({ launcher }: { launcher: LauncherController }) {
  const { state, openBrowse } = launcher
  return (
    <div className="space-y-5">
      <Card className="surface rounded-2xl">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">llama-server binary</CardTitle>
        </CardHeader>
        <CardContent>
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

      <Gaming />
      <Startup />
    </div>
  )
}
