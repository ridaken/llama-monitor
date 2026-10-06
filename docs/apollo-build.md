# Supported Apollo build and recovery

Apollo source and Windows builds live in the public
[ridaken/Apollo fork](https://github.com/ridaken/Apollo/tree/codex/llama-monitor-integration).
The integration branch starts at installed Apollo 0.4.6 commit
`0cd32abaaa141d262477d039ac447b38fe99c394`. The upstream default branch stays
unchanged. Do not enable upstream workflows or auto-update the installed build.

The **Windows integration build** workflow tests the production session store
and produces a portable package. Every artifact includes `build-manifest.json`,
`SHA256SUMS`, the exact source commit, pinned submodule revisions, and test results.
Review the package source and verify its checksum before installing. A capability
field identifies the protocol; the manifest identifies the actual tested build.

Current candidate: Apollo source
[`565117ee71986c403714e276e969e5db3d200a43`](https://github.com/ridaken/Apollo/commit/565117ee71986c403714e276e969e5db3d200a43),
version `0.4.6-llama-auth.565117ee`, from
[Windows build 37533179792](https://github.com/ridaken/Apollo/actions/runs/37533179792).
Its `Apollo.zip` SHA-256 is
`d5ced82b492bb0667cf09b6ee0735018459dfe2bccc419e3b33fcd58c1eb9000`.
Compilation and all six native session tests passed. Installation, real browser
authentication during polling, and real Moonlight streaming acceptance are still
pending; this candidate is not yet declared ready for everyday rollout.

The monitor checks compatibility before logging in and refuses stock builds.
The fork uses independent 30-day sessions, stores only cookie hashes in memory,
and preserves existing sessions when another client logs in. Password changes
invalidate every session, and Apollo restarts drop them. HTTP 503 at the 256-session
limit does not evict existing sessions. The monitor never exports its cookie.

## Install a tested package

1. End all Moonlight streams. Installation restarts Apollo and invalidates current
   web sessions, so one fresh browser login afterward is expected.
2. Download a successful build's artifact. Extract the artifact envelope to get
   the package ZIP and its manifest/checksum files. Do not extract the package
   over the live installation manually.
3. In an Administrator PowerShell window, run:

   ```powershell
   .\scripts\apollo-build.ps1 -Action Install -PackagePath 'C:\path\Apollo.zip' -ManifestPath 'C:\path\build-manifest.json' -ConfirmedDisconnected
   ```

   `-ConfirmedDisconnected` is your explicit confirmation that streams are ended;
   no authenticated API can establish this against stock Apollo without replacing
   its browser session. The installer also rejects any connected clients reported
   by the existing monitor. Unknown monitor status never counts as confirmation.

4. The installer verifies the package SHA-256 and rejects unsafe ZIP paths. It
   backs up program files and configuration, stops only the registered Apollo
   service, and replaces `sunshine.exe`, `zlib1.dll`, and packaged assets. It leaves
   drivers, service registration, pairing, application settings, and scripts alone.
5. Restart llama-monitor to load the compatibility guard. Test the local Apollo
   connection, then validate browser logins and Moonlight switching before enabling
   the integration for everyday use. Hook installation is a separate operation.

The installer records backup paths in an installation manifest. If replacement
or service startup fails, it restores previous program files before returning an
error. This custom package is unsigned; no official Apollo release is substituted.
The manifest is replaced atomically and records expected program checksums before
replacement. The same rollback command can recover an interrupted installation.

## Rollback

With all streams disconnected, run in Administrator PowerShell:

```powershell
.\scripts\apollo-build.ps1 -Action Rollback -BackupPath 'C:\path\backup-directory' -ConfirmedDisconnected
```

Rollback restores old program files and removes only files introduced by this
installation. It preserves current configuration and pairing; the original
configuration snapshot remains available in the backup directory. It refuses
rollback if a deployed program file has subsequently changed. Against stock
Apollo, the updated monitor refuses login and holds restoration rather than
restarting the browser logout cycle. Hook rollback is separately documented in
the README (`python apollo_setup.py --rollback`).

## Acceptance

Passing CI does not establish readiness for gaming on this PC. Record the tested
source commit after repeated browser logins during polling, Desktop and Steam Big
Picture streaming, multiple clients, brief reconnects, reconnect during model
loading, and backend/Apollo restarts. Confirm both original AI endpoints return,
memory drops during gaming, and failed restoration retains the original settings.
