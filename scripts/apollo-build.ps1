#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Rollback')][string]$Action,
    [string]$PackagePath,
    [string]$ManifestPath,
    [string]$BackupPath,
    [string]$InstallPath = 'C:\Program Files\Apollo',
    [ValidatePattern('^[A-Za-z0-9_-]+$')][string]$ServiceName = 'ApolloService',
    [switch]$ConfirmedDisconnected
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Inside-Path([string]$Root, [string]$Relative) {
    if ([IO.Path]::IsPathRooted($Relative) -or $Relative.Contains(':')) { throw 'Unsafe package path.' }
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $targetPath = [IO.Path]::GetFullPath([IO.Path]::Combine($rootPath, $Relative))
    if (-not $targetPath.StartsWith($rootPath, [StringComparison]::OrdinalIgnoreCase)) { throw 'Path escapes its target directory.' }
    return $targetPath
}

function File-Sha([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try { return Stream-Sha $stream }
    finally { $stream.Dispose() }
}

function Stream-Sha($Stream) {
    $digest = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($digest.ComputeHash($Stream)).Replace('-', '').ToLowerInvariant() }
    finally { $digest.Dispose() }
}

function Stop-Apollo {
    Stop-Service -Name $ServiceName -ErrorAction Stop
    (Get-Service -Name $ServiceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $remaining = @(Get-CimInstance Win32_Process -Filter "name='sunshine.exe'" | Where-Object {
            $_.ExecutablePath -and [IO.Path]::GetFullPath($_.ExecutablePath) -eq (Join-Path $InstallPath 'sunshine.exe')
        })
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Apollo process shutdown was not confirmed; no program files were replaced.'
}

function Start-Apollo {
    Start-Service -Name $ServiceName
    (Get-Service -Name $ServiceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
}

function Restore-Programs($Entries, [string]$Source) {
    foreach ($entry in $Entries) {
        $target = Inside-Path $InstallPath $entry.relative
        if ($entry.existed) {
            $original = Inside-Path (Join-Path $Source 'program') $entry.relative
            if ((File-Sha $original) -ne $entry.original_sha256) { throw 'Backup checksum mismatch.' }
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            Copy-Item -LiteralPath $original -Destination $target -Force
        } elseif (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Force
        }
    }
}

if (-not $ConfirmedDisconnected) { throw 'End every Moonlight stream, then pass -ConfirmedDisconnected.' }
$InstallPath = (Resolve-Path -LiteralPath $InstallPath).Path.TrimEnd('\')
$installMutex = New-Object System.Threading.Mutex($false, 'Global\llama-monitor-apollo-install')
$ownsInstallLock = $false
try {
try { $ownsInstallLock = $installMutex.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $ownsInstallLock = $true }
if (-not $ownsInstallLock) { throw 'Another Apollo installation or rollback is running.' }
$service = Get-CimInstance Win32_Service -Filter "Name='$($ServiceName.Replace("'", "''"))'"
if (-not $service -or $service.PathName.Trim('"') -ne (Join-Path $InstallPath 'tools\sunshinesvc.exe')) {
    throw 'The selected service does not belong to this Apollo installation.'
}
try {
    $monitorState = Invoke-RestMethod -Uri 'http://127.0.0.1:8500/api/gaming/state' -TimeoutSec 2
    if ($null -ne $monitorState.connected_clients -and $monitorState.connected_clients -gt 0) {
        throw 'Moonlight clients are connected. End the streams before installation.'
    }
} catch {
    if ($_.Exception.Message -like 'Moonlight clients*') { throw }
    # Explicit operator confirmation is required even when monitoring is unavailable.
}
$wasRunning = $service.State -eq 'Running'

if ($Action -eq 'Install') {
    $PackagePath = (Resolve-Path -LiteralPath $PackagePath).Path
    $build = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ($build.source_repository -ne 'https://github.com/ridaken/Apollo' -or
        $build.auth_sessions -ne 'multiple-v1' -or $build.source_commit -notmatch '^[0-9a-f]{40}$' -or
        $build.upstream_base -ne '0cd32abaaa141d262477d039ac447b38fe99c394') { throw 'Unsupported build manifest.' }
    $package = @($build.packages | Where-Object { $_.name -eq [IO.Path]::GetFileName($PackagePath) })
    if ($package.Count -ne 1 -or (File-Sha $PackagePath) -ne $package[0].sha256) { throw 'Package checksum mismatch.' }
    $zip = [IO.Compression.ZipFile]::OpenRead($PackagePath)
    try {
        $entries = @()
        $seen = @{}
        foreach ($item in $zip.Entries) {
            $relative = $item.FullName.Replace('/', '\')
            $null = Inside-Path $InstallPath $relative
            if ($relative.EndsWith('\')) { continue }
            # CPack ZIP has a single enclosing directory. Remove it after validating.
            if ($relative -notmatch '^(sunshine\.exe|zlib1\.dll|assets\\)' -and $relative.Contains('\')) {
                $relative = $relative.Substring($relative.IndexOf('\') + 1)
            }
            $target = Inside-Path $InstallPath $relative
            if ($relative -notmatch '^(sunshine\.exe$|zlib1\.dll$|assets\\)') { continue }
            if ($seen.ContainsKey($relative.ToLowerInvariant())) { throw 'Duplicate package destination.' }
            $seen[$relative.ToLowerInvariant()] = $true
            $entryStream = $item.Open()
            try { $installedHash = Stream-Sha $entryStream } finally { $entryStream.Dispose() }
            $entries += @{ relative = $relative; zip_entry = $item.FullName; existed = (Test-Path -LiteralPath $target); original_sha256 = $null; installed_sha256 = $installedHash }
        }
        if (-not $seen.ContainsKey('sunshine.exe')) { throw 'Package contains no Apollo executable.' }
        if (-not $PSCmdlet.ShouldProcess($InstallPath, 'Back up and install the verified Apollo integration package')) { return }
        if (-not $BackupPath) {
            $BackupPath = Join-Path $env:LOCALAPPDATA ('llama-monitor\apollo-backups\' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N'))
        }
        $BackupPath = [IO.Path]::GetFullPath($BackupPath)
        if (Test-Path -LiteralPath $BackupPath) { throw 'Backup directory must not already exist.' }
        New-Item -ItemType Directory -Path $BackupPath | Out-Null
        Copy-Item -LiteralPath (Join-Path $InstallPath 'config') -Destination (Join-Path $BackupPath 'config') -Recurse
        foreach ($entry in $entries) {
            if ($entry.existed) {
                $original = Inside-Path $InstallPath $entry.relative
                $destination = Inside-Path (Join-Path $BackupPath 'program') $entry.relative
                New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
                Copy-Item -LiteralPath $original -Destination $destination
                $entry.original_sha256 = File-Sha $destination
            }
        }
        $record = @{ install_path = $InstallPath; service_name = $ServiceName; source_commit = $build.source_commit; entries = $entries; status = 'backed-up' }
        $recordPath = Join-Path $BackupPath 'installation.json'
        $record | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $recordPath -Encoding utf8
        try {
            Stop-Apollo
            foreach ($entry in $entries) {
                $target = Inside-Path $InstallPath $entry.relative
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($zip.GetEntry($entry.zip_entry), $target, $true)
                if ((File-Sha $target) -ne $entry.installed_sha256) { throw 'Extracted program checksum mismatch.' }
            }
            if ($wasRunning) { Start-Apollo }
            $record.status = 'installed'
            $record | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $recordPath -Encoding utf8
        } catch {
            $failure = $_
            Stop-Apollo
            Restore-Programs $entries $BackupPath
            if ($wasRunning) { Start-Apollo }
            $record.status = 'rolled-back-after-failure'
            $record | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $recordPath -Encoding utf8
            throw $failure
        }
        Write-Output "Apollo integration build installed. Source: $($build.source_commit). Backup: $BackupPath"
    } finally { $zip.Dispose() }
} else {
    $BackupPath = (Resolve-Path -LiteralPath $BackupPath).Path
    $record = Get-Content -LiteralPath (Join-Path $BackupPath 'installation.json') -Raw | ConvertFrom-Json
    if ($record.install_path -ne $InstallPath -or $record.service_name -ne $ServiceName -or $record.status -notin @('installed', 'backed-up')) { throw 'Backup does not match the installed build.' }
    foreach ($entry in $record.entries) {
        $current = Inside-Path $InstallPath $entry.relative
        if (Test-Path -LiteralPath $current) {
            $currentHash = File-Sha $current
            if ($currentHash -ne $entry.installed_sha256 -and
                -not ($record.status -eq 'backed-up' -and $entry.existed -and $currentHash -eq $entry.original_sha256)) {
                throw 'Installed program files changed; refusing to overwrite later updates.'
            }
        } elseif ($record.status -ne 'backed-up' -or $entry.existed) {
            throw 'Installed program files changed; refusing to overwrite later updates.'
        }
        if ($entry.existed -and (File-Sha (Inside-Path (Join-Path $BackupPath 'program') $entry.relative)) -ne $entry.original_sha256) { throw 'Backup checksum mismatch.' }
    }
    if (-not $PSCmdlet.ShouldProcess($InstallPath, 'Restore the backed-up Apollo program files, preserving current configuration')) { return }
    Stop-Apollo
    Restore-Programs $record.entries $BackupPath
    if ($wasRunning) { Start-Apollo }
    $record.status = 'rolled-back'
    $record | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath (Join-Path $BackupPath 'installation.json') -Encoding utf8
    Write-Output 'Apollo program files restored. Current configuration and pairing were preserved.'
}
} finally {
    if ($ownsInstallLock) { $installMutex.ReleaseMutex() }
    $installMutex.Dispose()
}
