param(
    [Parameter(Mandatory=$true)][ValidateSet('prepare','session-ended')][string]$Action,
    [Parameter(Mandatory=$true)][string]$ConfigPath
)
$ErrorActionPreference = 'Stop'
try {
    $integrationConfig = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    Add-Type -AssemblyName System.Security
    $secretBytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
        [Convert]::FromBase64String($integrationConfig.token), $null,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    $hookToken = [System.Text.Encoding]::UTF8.GetString($secretBytes)
    $headers = @{ 'X-Llama-Monitor-Token' = $hookToken }
    $mutex = New-Object System.Threading.Mutex($false, ('Local\llama-monitor-hook-' + $integrationConfig.port))
    $ownsMutex = $false
    try {
        try { $ownsMutex = $mutex.WaitOne(20000) }
        catch [System.Threading.AbandonedMutexException] { $ownsMutex = $true }
        if (-not $ownsMutex) { throw 'Timed out waiting for llama-monitor startup.' }
        $backendReady = $false
        try {
            $response = Invoke-RestMethod -Uri ($integrationConfig.url + '/api/gaming/state') -TimeoutSec 2
            $backendReady = $true
        } catch {
            # Only start this project's backend. No model or default config is launched.
            Start-Process -FilePath $integrationConfig.python -ArgumentList @(
                ('"' + $integrationConfig.app + '"'), '--port', $integrationConfig.port,
                '--host', '127.0.0.1'
            ) -WorkingDirectory $integrationConfig.root -WindowStyle Hidden
        }
        if (-not $backendReady) {
            $startupDeadline = [DateTime]::UtcNow.AddSeconds(15)
            while ([DateTime]::UtcNow -lt $startupDeadline) {
                try {
                    $response = Invoke-RestMethod -Uri ($integrationConfig.url + '/api/gaming/state') -TimeoutSec 2
                    $backendReady = $true
                    break
                } catch { Start-Sleep -Milliseconds 250 }
            }
        }
        if (-not $backendReady) { throw 'llama-monitor backend did not become available.' }
    } finally {
        if ($ownsMutex) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
    $response = Invoke-RestMethod -Method Post -Uri ($integrationConfig.url + '/api/gaming/' + $Action) `
        -Headers $headers -TimeoutSec 45
    exit 0
} catch {
    # Never print tokens, credentials, or the request headers.
    [Console]::Error.WriteLine('llama-monitor Moonlight hook failed. Check the dashboard and Windows account permissions.')
    exit 1
}
