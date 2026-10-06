param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$startupConfig = $null
$startupCredential = $null
$accountPassword = $null
$pathsValidated = $false
try {
    $startupConfig = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    $setupDirectory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ConfigPath))
    foreach ($setupPath in @($startupConfig.result_path, $startupConfig.xml_path)) {
        if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($setupPath)) -ne $setupDirectory) {
            throw 'Setup files must stay in the llama-monitor state directory.'
        }
    }
    $pathsValidated = $true
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $folder = $scheduler.GetFolder('\')
    $existingTask = $null
    try { $existingTask = $folder.GetTask($startupConfig.task_name) }
    catch [System.Runtime.InteropServices.COMException] {
        if ($_.Exception.HResult -ne -2147024894) { throw }
    }
    if ($existingTask) {
        $definition = $existingTask.Definition
        $existingOwner = $definition.Principal.UserId
        if (-not $existingOwner.StartsWith('S-1-')) {
            $existingOwner = (New-Object Security.Principal.NTAccount($existingOwner)).Translate([Security.Principal.SecurityIdentifier]).Value
        }
        if ($definition.RegistrationInfo.Description -ne $startupConfig.description -or
            $existingOwner -ne $startupConfig.sid) {
            throw 'A task with this name exists and belongs to another setup. It was left unchanged.'
        }
    }
    if ($startupConfig.action -eq 'remove') {
        if ($existingTask) { $folder.DeleteTask($startupConfig.task_name, 0) }
    } elseif ($startupConfig.action -eq 'install') {
        $taskXml = Get-Content -LiteralPath $startupConfig.xml_path -Raw
        [xml]$parsedTask = $taskXml
        $principal = $parsedTask.Task.Principals.Principal
        $expectedLogonType = if ($startupConfig.mode -eq 'boot') { 'Password' } else { 'InteractiveToken' }
        if ($principal.RunLevel -ne 'LeastPrivilege' -or $principal.UserId -ne $startupConfig.sid -or
            $principal.LogonType -ne $expectedLogonType -or
            $startupConfig.sid -in @('S-1-5-18','S-1-5-19','S-1-5-20')) {
            throw 'Startup must run under the original user account with limited privileges.'
        }
        if ($startupConfig.mode -eq 'boot') {
            # Windows PowerShell's native credential dialog. No password goes
            # into the config file, task XML, command line or HTTP request.
            $startupCredential = Get-Credential -UserName $startupConfig.account -Message 'Enter your Windows account password (not your PIN) so llama-monitor can start before sign-in.'
            if (-not $startupCredential) { throw 'Windows credential entry was cancelled.' }
            $credentialAccount = New-Object Security.Principal.NTAccount($startupCredential.UserName)
            $credentialSid = $credentialAccount.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($credentialSid -ne $startupConfig.sid) { throw 'Use the same Windows account as llama-monitor.' }
            $accountPassword = $startupCredential.GetNetworkCredential().Password
            Register-ScheduledTask -TaskName $startupConfig.task_name -Xml $taskXml `
                -User $startupConfig.account -Password $accountPassword -Force | Out-Null
        } else {
            if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne $startupConfig.sid) {
                throw 'Sign-in setup must be approved under the same Windows account as llama-monitor.'
            }
            Register-ScheduledTask -TaskName $startupConfig.task_name -Xml $taskXml -Force | Out-Null
        }
        # Verify the definition Windows actually registered.
        $registered = $folder.GetTask($startupConfig.task_name).Definition
        if ($registered.Settings.ExecutionTimeLimit -ne 'PT0S' -or
            $registered.Principal.RunLevel -ne 0 -or $registered.Actions.Count -ne 1) {
            throw 'Windows registered unexpected task settings; review startup in Task Scheduler.'
        }
    } else { throw 'Invalid startup setup action.' }
    $result = @{operation=$startupConfig.operation;success=$true}
} catch {
    # Account passwords are never echoed. Windows error codes aid diagnosis.
    $result = @{operation=$startupConfig.operation;success=$false;error=('Windows startup setup failed or was cancelled. Error code: ' + $_.Exception.HResult)}
} finally {
    $accountPassword = $null
    $startupCredential = $null
}
if ($startupConfig -and $pathsValidated) {
    $resultTemp = $startupConfig.result_path + '.tmp'
    # Both resolved absolute paths were verified within the setup directory.
    $result | ConvertTo-Json | Set-Content -LiteralPath $resultTemp -Encoding UTF8
    Move-Item -LiteralPath $resultTemp -Destination $startupConfig.result_path -Force
}
