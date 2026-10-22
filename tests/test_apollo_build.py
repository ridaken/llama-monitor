"""Exercise the Windows installer against isolated files and mocked services."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows installation helper")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "apollo-build.ps1"


def quoted(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "Apollo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "apps.json").write_text("original settings")
    (root / "sunshine.exe").write_bytes(b"original executable")
    (root / "assets").mkdir()
    (root / "assets" / "existing.txt").write_text("original asset")
    package = tmp_path / "Apollo.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Apollo/sunshine.exe", b"integration executable")
        archive.writestr("Apollo/assets/existing.txt", "new asset")
        archive.writestr("Apollo/assets/new.txt", "added asset")
        archive.writestr("Apollo/config/apps.json", "must never overwrite settings")
        archive.writestr("Apollo/scripts/uninstall-service.bat", "must never run")
    manifest = tmp_path / "build-manifest.json"
    manifest.write_text(json.dumps({"source_repository": "https://github.com/ridaken/Apollo",
        "source_commit": "a" * 40, "upstream_base": "0cd32abaaa141d262477d039ac447b38fe99c394",
        "auth_sessions": "multiple-v1",
        "packages": [{"name": package.name, "sha256": hashlib.sha256(package.read_bytes()).hexdigest()}]}))
    return root, package, manifest, tmp_path / "backup"


def run_installer(installation, *, action="Install", clients=0, fail_start=False, confirmed=True, whatif=False):
    root, package, manifest, backup = installation
    # Strip elevation requirement only in this mock harness. Never invoke real
    # service operations; scriptblocks get the same file/ZIP logic as deployment.
    script = SCRIPT.read_text().replace("#Requires -RunAsAdministrator", "")
    harness = root.parent / "exercise.ps1"
    args = (f"-Action {action} -InstallPath {quoted(root)} -PackagePath {quoted(package)} "
            f"-ManifestPath {quoted(manifest)} -BackupPath {quoted(backup)}"
            + (" -ConfirmedDisconnected" if confirmed else "") + (" -WhatIf" if whatif else ""))
    harness.write_text(f"""
$ErrorActionPreference = 'Stop'
function Get-CimInstance {{
    param($ClassName, $Filter)
    if ($ClassName -eq 'Win32_Service') {{
        return [pscustomobject]@{{ PathName = {quoted(root / 'tools' / 'sunshinesvc.exe')}; State = 'Running' }}
    }}
    return @()
}}
function Get-Service {{
    param($Name)
    if ($Name -ne 'ApolloService') {{ throw 'Unrelated service accessed' }}
    $service = [pscustomobject]@{{ Name = $Name }}
    $service | Add-Member -MemberType ScriptMethod -Name WaitForStatus -Value {{ param($state, $timeout) }}
    return $service
}}
function Stop-Service {{ param($Name, $ErrorAction) if ($Name -ne 'ApolloService') {{ throw 'Unrelated stop' }} }}
$script:failStart = {'$true' if fail_start else '$false'}
function Start-Service {{
    param($Name)
    if ($Name -ne 'ApolloService') {{ throw 'Unrelated start' }}
    if ($script:failStart) {{ $script:failStart = $false; throw 'Simulated startup failure' }}
}}
function Invoke-RestMethod {{ param($Uri, $TimeoutSec) return [pscustomobject]@{{ connected_clients = {clients} }} }}
try {{
    & ([scriptblock]::Create(@'
{script}
'@)) {args}
}} catch {{ Write-Output $_.Exception.Message; exit 1 }}
""", encoding="utf-8")
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                          capture_output=True, text=True, timeout=30)


def test_install_and_rollback_preserve_current_config(installation):
    root, _, _, backup = installation
    result = run_installer(installation)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "sunshine.exe").read_bytes() == b"integration executable"
    assert (root / "config" / "apps.json").read_text() == "original settings"
    assert not (root / "scripts").exists()
    assert (backup / "config" / "apps.json").read_text() == "original settings"
    (root / "config" / "apps.json").write_text("later settings")
    result = run_installer(installation, action="Rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "sunshine.exe").read_bytes() == b"original executable"
    assert (root / "assets" / "existing.txt").read_text() == "original asset"
    assert not (root / "assets" / "new.txt").exists()
    assert (root / "config" / "apps.json").read_text() == "later settings"


@pytest.mark.parametrize("clients,confirmed", [(1, True), (0, False)])
def test_connection_or_missing_confirmation_prevents_mutation(installation, clients, confirmed):
    result = run_installer(installation, clients=clients, confirmed=confirmed)
    assert result.returncode != 0
    assert installation[0].joinpath("sunshine.exe").read_bytes() == b"original executable"
    assert not installation[3].exists()


def test_checksum_failure_prevents_mutation(installation):
    installation[1].write_bytes(b"modified package")
    result = run_installer(installation)
    assert result.returncode != 0 and "checksum" in result.stdout
    assert not installation[3].exists()


def test_zip_traversal_prevents_mutation(installation):
    _, package, manifest, backup = installation
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("../../outside.txt", "unsafe")
    build = json.loads(manifest.read_text())
    build["packages"][0]["sha256"] = hashlib.sha256(package.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(build))
    result = run_installer(installation)
    assert result.returncode != 0 and "escapes" in result.stdout
    assert not backup.exists()
    assert not package.parent.joinpath("outside.txt").exists()


def test_startup_failure_restores_program_files(installation):
    result = run_installer(installation, fail_start=True)
    assert result.returncode != 0 and "startup failure" in result.stdout
    assert installation[0].joinpath("sunshine.exe").read_bytes() == b"original executable"
    assert not installation[0].joinpath("assets/new.txt").exists()


def test_changed_program_blocks_rollback(installation):
    assert run_installer(installation).returncode == 0
    installation[0].joinpath("sunshine.exe").write_bytes(b"later update")
    result = run_installer(installation, action="Rollback")
    assert result.returncode != 0 and "changed" in result.stdout
    assert installation[0].joinpath("sunshine.exe").read_bytes() == b"later update"


def test_whatif_writes_no_files(installation):
    result = run_installer(installation, whatif=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not installation[3].exists()


def test_interrupted_installation_can_be_rolled_back(installation):
    assert run_installer(installation).returncode == 0
    root, _, _, backup = installation
    record_path = backup / 'installation.json'
    record = json.loads(record_path.read_text(encoding='utf-8-sig'))
    record['status'] = 'backed-up'
    record_path.write_text(json.dumps(record))
    root.joinpath('assets/new.txt').unlink()
    root.joinpath('assets/existing.txt').write_text('original asset')
    result = run_installer(installation, action='Rollback')
    assert result.returncode == 0, result.stdout + result.stderr
    assert root.joinpath('sunshine.exe').read_bytes() == b'original executable'
