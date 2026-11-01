<#
.SYNOPSIS
    Migrate a machine from stock RustDesk to the DataSoftware Remote client,
    keeping its ID, its key and its permanent password.

.DESCRIPTION
    Installs the DataSoftware client, adopts the stock client's identity,
    verifies the result against the ID server, and only then removes stock.

    Why adopt rather than let it enrol fresh: the peer ID is derived from the
    machine, so both clients compute the same one, but each generates its own
    key pair. The second one to register is refused with "PK mismatch" and stays
    unreachable - while /api/sysinfo, a separate HTTPS channel, keeps working,
    so the console shows the new version and the machine looks migrated when it
    is not.

    Config values are encrypted with a key derived from the machine uid, not
    from the application name, so the stock blobs decrypt in the new client and
    the identity can simply be copied. The server then sees the same ID with the
    public key it already stored: no mismatch, no approval, and no interruption.

    Windows 10, 11 and Server 2016+ with Windows PowerShell 5.1. Nothing here
    needs PowerShell 7.

.PARAMETER MsiPath
    Use a local MSI instead of downloading. Worth it across many machines:
    fetch it once, put it on a share, pass the path.

.PARAMETER KeepStock
    Do everything except remove stock RustDesk. Use for the first machine, or
    whenever you want to look before committing.

.PARAMETER VerifySeconds
    How long to watch the client log for registration refusals before deciding
    it worked. Registration is retried about every 16 s, so keep this above 35.

.PARAMETER Force
    Remove stock even if verification failed. Only with a way back into the
    machine that does not depend on remote access.

.EXAMPLE
    .\Migrate-ToDataSoftware.ps1
    .\Migrate-ToDataSoftware.ps1 -MsiPath \\nas\sw\datasoftware-remote.msi
    .\Migrate-ToDataSoftware.ps1 -KeepStock
#>

#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string] $MsiPath,
    [switch] $KeepStock,
    [ValidateRange(35, 600)]
    [int]    $VerifySeconds = 60,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
$AppName      = 'DataSoftware-Remote'
$StockName    = 'RustDesk'
$Repo         = 'DATA-SOFTWARE-s-r-o/rustdesk'
$AckKey       = 'datasoftware-initial-password-acknowledged'
$RefusalRegex = 'server refused to register this device|unknown RegisterPkResponse'
$TsRegex      = '^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})'

$Stamp      = Get-Date -Format 'yyyyMMdd-HHmmss'
$Transcript = Join-Path $env:WINDIR "Temp\datasoftware-migration-$env:COMPUTERNAME-$Stamp.log"

# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------
$script:StepNo = 0
function Write-Step ($m) { $script:StepNo++; Write-Host ''; Write-Host ("[{0}] {1}" -f $script:StepNo, $m) -ForegroundColor Cyan }
function Write-Ok   ($m) { Write-Host "    ok   $m" -ForegroundColor Green }
function Write-Info ($m) { Write-Host "         $m" -ForegroundColor Gray }
function Write-Warn ($m) { Write-Host "    !    $m" -ForegroundColor Yellow }
function Write-Bad  ($m) { Write-Host "    X    $m" -ForegroundColor Red }

# --------------------------------------------------------------------------
# Config plumbing
# --------------------------------------------------------------------------

# The current log is held open by the running service, so it cannot be read
# through File::ReadLines - that throws "being used by another process".
function Read-SharedLines {
    param([string] $Path)
    $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
                                 [System.IO.FileAccess]::Read,
                                 [System.IO.FileShare]::ReadWrite)
    try {
        $sr = New-Object System.IO.StreamReader($fs)
        try   { while ($null -ne ($line = $sr.ReadLine())) { $line } }
        finally { $sr.Dispose() }
    } finally { $fs.Dispose() }
}

# The flag belongs in the [options] table. Appending it to the end of the file
# puts it in whatever table happens to be last ([ui_flutter]), where
# LocalConfig::get_option never looks - and the first-run dialog then generates
# a fresh password straight over the one just adopted.
function Set-AckFlag {
    param([string] $Path)
    $line = "$AckKey = 'Y'"

    $dir = Split-Path $Path -Parent
    if (-not (Test-PathSafe $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    if (-not (Test-PathSafe $Path)) {
        Set-Content -Path $Path -Value "[options]`r`n$line" -Encoding UTF8
        return 'created the file'
    }

    $lines = [string[]] @(Get-Content -Path $Path)
    $opt   = [Array]::FindIndex($lines, [Predicate[string]] { $args[0].Trim() -eq '[options]' })

    if ($opt -lt 0) {
        Add-Content -Path $Path -Value "`r`n[options]`r`n$line"
        return 'added an [options] table'
    }

    $end = [Array]::FindIndex($lines, $opt + 1, [Predicate[string]] { $args[0].Trim() -match '^\[' })
    if ($end -lt 0) { $end = $lines.Count }

    for ($i = $opt + 1; $i -lt $end; $i++) {
        if ($lines[$i] -match "^\s*$AckKey\s*=") {
            $lines[$i] = $line
            Set-Content -Path $Path -Value $lines -Encoding UTF8
            return 'already present, refreshed'
        }
    }

    $new = @($lines[0..$opt]) + $line + @($lines[($opt + 1)..($lines.Count - 1)])
    Set-Content -Path $Path -Value $new -Encoding UTF8
    return 'inserted into [options]'
}

function Test-AckFlag {
    param([string] $Path)
    if (-not (Test-PathSafe $Path)) { return $false }
    $l = [string[]] @(Get-Content $Path)
    $o = [Array]::FindIndex($l, [Predicate[string]] { $args[0].Trim() -eq '[options]' })
    if ($o -lt 0) { return $false }
    $n = [Array]::FindIndex($l, $o + 1, [Predicate[string]] { $args[0].Trim() -match '^\[' })
    if ($n -lt 0) { $n = $l.Count }
    $k = [Array]::FindIndex($l, [Predicate[string]] { $args[0] -match [regex]::Escape($AckKey) })
    return ($k -gt $o -and $k -lt $n)
}

# Test-Path throws on a profile whose ACLs exclude us, and with
# $ErrorActionPreference = 'Stop' that would abort the whole migration. On a
# machine with several user profiles this is the normal case, not an edge one.
function Test-PathSafe {
    param([string] $Path)
    try { return [bool] (Test-Path -LiteralPath $Path -ErrorAction Stop) }
    catch { return $false }
}

# A directory that certainly exists and is writable.
#
# RustDesk's elevated terminal runs as SYSTEM, and the SYSTEM profile often has
# no AppData\Local\Temp at all - $env:TEMP then points at a path that is simply
# not there and the download fails with DirectoryNotFoundException.
# C:\Windows\Temp always exists.
function Get-ScratchDir {
    foreach ($d in @($env:TEMP, (Join-Path $env:WINDIR 'Temp'))) {
        if ($d -and (Test-PathSafe $d)) { return $d }
    }
    $d = Join-Path $env:WINDIR 'Temp'
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}

# Every profile that either client has touched.
#
# Not just $env:APPDATA: on a workstation the technician often elevates with a
# different account than the one logged in, so $env:APPDATA points at the admin
# profile while the dialog will appear in the logged-in user's session. Miss
# that profile and the dialog still fires and overwrites the adopted password.
function Get-ProfilePairs {
    $usersRoot = Split-Path $env:PUBLIC -Parent     # normally C:\Users
    $roots = New-Object System.Collections.Generic.List[string]

    if (Test-PathSafe $usersRoot) {
        Get-ChildItem $usersRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $roots.Add((Join-Path $_.FullName 'AppData\Roaming'))
        }
    }
    $roots.Add("$env:WINDIR\ServiceProfiles\LocalService\AppData\Roaming")
    $roots.Add("$env:WINDIR\ServiceProfiles\NetworkService\AppData\Roaming")
    $roots.Add("$env:WINDIR\System32\config\systemprofile\AppData\Roaming")

    foreach ($r in ($roots | Select-Object -Unique)) {
        $src = Join-Path $r "$StockName\config\$StockName.toml"
        $dst = Join-Path $r "$AppName\config\$AppName.toml"
        $ack = Join-Path $r "$AppName\config\${AppName}_local.toml"
        if ((Test-PathSafe $src) -or (Test-PathSafe $dst)) {
            [pscustomobject]@{
                Name = $r.Replace('\AppData\Roaming', '')
                Src  = $src
                Dst  = $dst
                Ack  = $ack
            }
        }
    }
}

# --------------------------------------------------------------------------
# Service plumbing
# --------------------------------------------------------------------------
function Stop-Client {
    if (Get-Service -Name $AppName -ErrorAction SilentlyContinue) {
        & sc.exe stop $AppName | Out-Null
        for ($i = 0; $i -lt 15; $i++) {
            $s = Get-Service -Name $AppName -ErrorAction SilentlyContinue
            if (-not $s -or $s.Status -eq 'Stopped') { break }
            Start-Sleep -Seconds 1
        }
    }
    Get-Process -Name $AppName -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

function Start-Client {
    & sc.exe start $AppName | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        $s = Get-Service -Name $AppName -ErrorAction SilentlyContinue
        if ($s -and $s.Status -eq 'Running') { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

# key_confirmed in the config proves nothing here - it was copied in from the
# stock client. The log, judged by the timestamp on each line, is the honest
# source: a refused client repeats the refusal roughly every 16 seconds.
function Test-Registration {
    param([datetime] $Since, [int] $Seconds)

    $dirs = @("$env:WINDIR\ServiceProfiles\LocalService\AppData\Roaming\$AppName\log\server")
    foreach ($p in (Get-ProfilePairs)) {
        $dirs += (Join-Path $p.Name "AppData\Roaming\$AppName\log")
        $dirs += (Join-Path $p.Name "AppData\Roaming\$AppName\log\server")
    }

    $deadline = (Get-Date).AddSeconds($Seconds)
    $lastSeen = $null

    while ((Get-Date) -lt $deadline) {
        $remaining = [int]((($deadline) - (Get-Date)).TotalSeconds)
        Write-Progress -Activity 'Watching the client log for registration refusals' `
                       -Status "$remaining s left" `
                       -PercentComplete ([Math]::Max(0, 100 - ($remaining * 100 / $Seconds)))
        foreach ($d in ($dirs | Select-Object -Unique)) {
            if (-not (Test-PathSafe $d)) { continue }
            foreach ($f in Get-ChildItem $d -Filter *.log -ErrorAction SilentlyContinue) {
                if ($f.LastWriteTime -lt $Since) { continue }
                try { $lines = Read-SharedLines $f.FullName } catch { continue }
                foreach ($line in $lines) {
                    if ($line -notmatch $RefusalRegex) { continue }
                    if ($line -notmatch $TsRegex)      { continue }
                    $ts = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
                    if ($ts -gt $Since) { $lastSeen = $ts }
                }
            }
        }
        if ($lastSeen) { break }
        Start-Sleep -Seconds 5
    }
    Write-Progress -Activity 'Watching the client log' -Completed

    return [pscustomobject]@{ Ok = ($null -eq $lastSeen); LastRefusal = $lastSeen }
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
$backups  = @()
$adopted  = 0
$verified = $false

Start-Transcript -Path $Transcript -Force | Out-Null
try {
    Write-Host ''
    Write-Host "DataSoftware Remote migration   $env:COMPUTERNAME   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor White
    Write-Host "transcript: $Transcript" -ForegroundColor DarkGray

    # ---- 1. survey -------------------------------------------------------
    Write-Step 'Surveying the machine'

    $os = Get-CimInstance Win32_OperatingSystem
    Write-Info ("windows      : {0} ({1})" -f $os.Caption.Trim(), $os.BuildNumber)
    Write-Info ("powershell   : {0}" -f $PSVersionTable.PSVersion)

    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($arch -eq 'x86' -and -not $env:PROCESSOR_ARCHITEW6432) {
        throw 'this is 32-bit Windows; only an x86_64 build is published'
    }
    if ($arch -eq 'ARM64') { Write-Warn 'ARM64 Windows - the x64 build runs emulated and is not tested here' }

    $stockSvc  = Get-Service -Name $StockName -ErrorAction SilentlyContinue
    $stockReg  = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$StockName" -ErrorAction SilentlyContinue
    $clientSvc = Get-Service -Name $AppName -ErrorAction SilentlyContinue
    $pairs     = @(Get-ProfilePairs)
    $withStock = @($pairs | Where-Object { Test-PathSafe $_.Src })

    if ($stockSvc) { Write-Info "stock service: $($stockSvc.Status)" } else { Write-Info 'stock service: not installed' }
    if ($clientSvc) { Write-Info "this client  : $($clientSvc.Status)" } else { Write-Info 'this client  : not installed' }
    Write-Info ("profiles     : {0} found, {1} with a stock identity" -f $pairs.Count, $withStock.Count)
    foreach ($p in $pairs) { Write-Info ("               {0}" -f $p.Name) }

    if ($withStock.Count -eq 0) {
        Write-Warn 'No stock identity to adopt - this machine will enrol as a new device.'
    }

    # ---- 2. install ------------------------------------------------------
    if ($clientSvc) {
        Write-Step 'Client already installed - skipping installation'
    } else {
        Write-Step 'Installing the DataSoftware client'

        if ($MsiPath) {
            if (-not (Test-Path $MsiPath)) { throw "MSI not found: $MsiPath" }
            $msi = (Resolve-Path $MsiPath).Path
            Write-Info "using $msi"
        } else {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $tag = $null
            for ($try = 1; $try -le 3; $try++) {
                try {
                    $tag = (Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$Repo/releases/latest").tag_name
                    break
                } catch {
                    Write-Warn "release lookup attempt $try failed: $($_.Exception.Message)"
                    Start-Sleep -Seconds (3 * $try)
                }
            }
            if (-not $tag) { throw 'could not resolve the latest release' }

            $url = "https://github.com/$Repo/releases/download/$tag/rustdesk-$tag-x86_64.msi"
            $msi = Join-Path (Get-ScratchDir) "datasoftware-remote-$tag.msi"
            Write-Info "downloading $tag"
            for ($try = 1; $try -le 3; $try++) {
                try { Invoke-WebRequest -UseBasicParsing $url -OutFile $msi; break }
                catch {
                    Write-Warn "download attempt $try failed: $($_.Exception.Message)"
                    Start-Sleep -Seconds (3 * $try)
                }
            }
            if (-not (Test-Path $msi)) { throw 'download failed' }
        }

        $size = (Get-Item $msi).Length
        if ($size -lt 10MB) { throw "the MSI is only $size bytes - truncated" }
        Write-Ok ('MSI ready, {0:n1} MB' -f ($size / 1MB))

        $msiLog = Join-Path $env:WINDIR "Temp\datasoftware-msi-$Stamp.log"
        $p = Start-Process msiexec -ArgumentList "/i `"$msi`" /qn /norestart /l*v `"$msiLog`"" -Wait -PassThru
        # 3010 is success with a reboot pending, which does not affect us here.
        if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "msiexec exited $($p.ExitCode) - see $msiLog" }
        Write-Ok "installed (msiexec $($p.ExitCode))"

        for ($i = 0; $i -lt 30 -and -not (Get-Service -Name $AppName -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Seconds 1 }
        if (-not (Get-Service -Name $AppName -ErrorAction SilentlyContinue)) { throw 'the service never appeared after installation' }
        Write-Ok 'service registered'

        # installation creates profile directories, so the survey is now stale
        $pairs     = @(Get-ProfilePairs)
        $withStock = @($pairs | Where-Object { Test-PathSafe $_.Src })
    }

    # ---- 3. adopt --------------------------------------------------------
    Write-Step 'Stopping the client and settling its identity'
    Stop-Client
    Write-Info 'client stopped'

    foreach ($pair in $pairs) {
        $label = Split-Path $pair.Name -Leaf

        if ((Test-PathSafe $pair.Src) -and (Test-PathSafe $pair.Dst)) {
            $bak = "$($pair.Dst).bak-$Stamp"
            Copy-Item $pair.Dst $bak -Force
            $backups += [pscustomobject]@{ Live = $pair.Dst; Backup = $bak }
            Copy-Item $pair.Src $pair.Dst -Force
            $adopted++
            Write-Ok "$label : identity adopted"
        } elseif (Test-PathSafe $pair.Src) {
            Write-Info "$label : stock only, nothing to adopt into yet"
        } else {
            Write-Info "$label : no stock identity here"
        }

        # Set the flag even where nothing was adopted: whichever user launches
        # the client first must not be shown the dialog, or it generates a new
        # password over the adopted one.
        $how = Set-AckFlag $pair.Ack
        if (-not (Test-AckFlag $pair.Ack)) { throw "the acknowledgement flag did not land in [options]: $($pair.Ack)" }
        Write-Info "$label : flag $how"
    }

    if ($withStock.Count -gt 0 -and $adopted -eq 0) {
        throw 'a stock identity exists but none could be adopted - refusing to continue'
    }

    # ---- 4. restart ------------------------------------------------------
    Write-Step 'Starting the client'

    # Restart the service by itself if it ever dies or fails to start. The
    # installer sets no recovery actions, and on one server a single start
    # that exceeded the 30 s SCM limit left it stopped for two days.
    & sc.exe failure $AppName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Ok 'recovery actions set (restart after 60 s, three times a day)' }
    else { Write-Warn "could not set recovery actions (sc exit $LASTEXITCODE)" }

    $restartedAt = Get-Date
    if (-not (Start-Client)) { throw 'the service did not reach Running' }
    Write-Ok 'service running'

    # ---- 5. verify -------------------------------------------------------
    Write-Step "Verifying registration (watching for $VerifySeconds s)"
    $v = Test-Registration -Since $restartedAt -Seconds $VerifySeconds
    if ($v.Ok) {
        $verified = $true
        Write-Ok 'no registration refusals - the server accepted this client'
    } else {
        Write-Bad "the server refused the client at $($v.LastRefusal)"
        Write-Info 'the identity was not accepted; stock is being left in place'
    }

    # ---- 6. remove stock -------------------------------------------------
    if (-not $stockReg) {
        Write-Step 'Stock RustDesk is not installed - nothing to remove'
    } elseif ($KeepStock) {
        Write-Step 'Leaving stock RustDesk installed (-KeepStock)'
    } elseif (-not $verified -and -not $Force) {
        Write-Step 'Not removing stock RustDesk - verification failed'
        Write-Warn 'Fix the registration first. Re-run this script, or use -Force only if you can reach the machine another way.'
    } else {
        Write-Step "Removing stock RustDesk ($($stockReg.DisplayVersion))"
        & sc.exe stop $StockName | Out-Null
        Start-Sleep -Seconds 3

        # Stock is installed either by its own installer or from an MSI, and the
        # two need different handling. `RustDesk.exe --uninstall` looks at its
        # own UninstallString first and, when that is an msiexec line, simply
        # runs it - without /qn, so Windows puts up a progress dialog with a
        # Cancel button. Drive msiexec ourselves in that case.
        $uninstallString = ''
        if ($stockReg.PSObject.Properties.Name -contains 'UninstallString') {
            $uninstallString = [string] $stockReg.UninstallString
        }

        $removed = $false
        if ($uninstallString -match 'msiexec' -and $uninstallString -match '(\{[0-9A-Fa-f-]{36}\})') {
            $code   = $Matches[1]
            $msiLog = Join-Path $env:WINDIR "Temp\rustdesk-uninstall-$Stamp.log"
            Write-Info "MSI install, removing $code quietly"
            $u = Start-Process msiexec -ArgumentList "/x $code /qn /norestart /l*v `"$msiLog`"" -Wait -PassThru
            # 1605 = that product is not installed, i.e. already gone.
            if ($u.ExitCode -eq 0 -or $u.ExitCode -eq 3010 -or $u.ExitCode -eq 1605) {
                $removed = $true
                Write-Ok "uninstalled (msiexec $($u.ExitCode))"
            } else {
                Write-Warn "msiexec exited $($u.ExitCode) - see $msiLog"
            }
        } else {
            $stockExe = Join-Path ${env:ProgramFiles} "$StockName\$StockName.exe"
            if (Test-Path $stockExe) {
                Write-Info 'self-install, calling its own uninstaller'
                Start-Process $stockExe -ArgumentList '--uninstall' -Wait
                $removed = $true
                Write-Ok 'uninstalled'
            } else {
                Write-Warn "could not find $stockExe - remove it by hand"
            }
        }

        if ($removed) {
            Start-Sleep -Seconds 5
            Write-Info 'its config is left in place on purpose - that is the way back to the old identity'
        }
    }

    # ---- summary ---------------------------------------------------------
    Write-Host ''
    Write-Host '--------------------------------------------------------------' -ForegroundColor White
    $svc   = Get-Service -Name $AppName -ErrorAction SilentlyContinue
    $stock = Get-Service -Name $StockName -ErrorAction SilentlyContinue
    if ($svc)   { Write-Host "  client service   : $($svc.Status)" }   else { Write-Host '  client service   : MISSING' }
    if ($adopted -gt 0) { Write-Host "  identity         : adopted from stock ($adopted profile(s))" }
    else                { Write-Host '  identity         : own (new device)' }
    if ($verified) { Write-Host '  registration     : accepted by the server' }
    else           { Write-Host '  registration     : NOT CONFIRMED' }
    if ($stock) { Write-Host '  stock RustDesk   : still installed' } else { Write-Host '  stock RustDesk   : removed' }
    Write-Host "  transcript       : $Transcript"
    Write-Host '--------------------------------------------------------------' -ForegroundColor White

    if ($verified) {
        Write-Host ''
        Write-Host '  Check the console: the device should be online under its usual ID,' -ForegroundColor Green
        Write-Host '  with the same permanent password as before.' -ForegroundColor Green
        exit 0
    } else {
        Write-Host ''
        Write-Host '  Registration was not confirmed. Stock is still there, so the machine' -ForegroundColor Yellow
        Write-Host '  is still reachable. Send the transcript before changing anything.' -ForegroundColor Yellow
        exit 2
    }
}
catch {
    Write-Host ''
    Write-Bad $_.Exception.Message

    if ($backups.Count -gt 0) {
        Write-Warn 'rolling the adopted config back'
        foreach ($b in $backups) {
            try { Copy-Item $b.Backup $b.Live -Force; Write-Info "restored $($b.Live)" }
            catch { Write-Bad "could not restore $($b.Live): $($_.Exception.Message)" }
        }
        Start-Client | Out-Null
    }

    Write-Host ''
    Write-Host '  Nothing was removed. The machine is still reachable through whatever' -ForegroundColor Yellow
    Write-Host "  was working before. Transcript: $Transcript" -ForegroundColor Yellow
    exit 1
}
finally {
    Stop-Transcript | Out-Null
}
