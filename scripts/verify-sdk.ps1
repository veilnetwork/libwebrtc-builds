<#
.SYNOPSIS
  Prove a win-x64 bundle builds the veil_media wrapper — with the WebRTC
  checkout that produced it renamed away.

.DESCRIPTION
  The Windows twin of verify-sdk.sh, and the -Hide rename is the whole point
  exactly as it is there: every include path in the compile command is
  relative, so a header the bundle forgot to carry resolves against the
  original tree and the test passes for a reason that will not exist on
  anyone else's machine. Rename the source of truth away and the test becomes
  real.

  The gate on the result is the exported symbol count, with a floor rather
  than an existence check: a wrapper that links but exports nothing is exactly
  the failure that ships an engine the app cannot call into.

  Unlike the other targets this one cannot be a few lines inside
  verify-sdk.sh: the veil wrapper build for Windows is a PowerShell script
  taking PowerShell parameters, and the bundle is laid out by setup.ps1
  rather than setup.sh (see pack_sdk.py for why).
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Bundle,
  [Parameter(Mandatory = $true)][string]$Target,
  [Parameter(Mandatory = $true)][string]$Veil,
  [string]$Hide = '',
  [string]$Dest = ''
)

$ErrorActionPreference = 'Stop'

if (-not $Dest) { $Dest = Join-Path ([System.IO.Path]::GetTempPath()) 'engine-out' }
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

$media = Join-Path $Veil 'flutter\veil_media'
if (-not (Test-Path (Join-Path $media 'src'))) { throw "no veil_media sources at $media" }
$builder = Join-Path $media 'windows\build_veil_media_dll_windows.ps1'
if (-not (Test-Path $builder)) { throw "no $builder" }

$setup = Join-Path $Bundle 'setup.ps1'
if (-not (Test-Path $setup)) { throw "the bundle ships no setup.ps1 - pack_sdk.py did not treat this as a windows target" }
& $setup

$hidden = ''
try {
  if ($Hide -and (Test-Path $Hide)) {
    $hidden = "$Hide.hidden-for-verify"
    if (Test-Path $hidden) { Remove-Item -Recurse -Force $hidden }
    Move-Item -LiteralPath $Hide -Destination $hidden
    Write-Host "==> hid $Hide - the bundle is now the only WebRTC on this machine"
    if (Test-Path $Hide) { throw "$Hide still exists after the rename" }
  }

  $env:WEBRTC_SRC = Join-Path $Bundle 'src'
  $env:WEBRTC_OUT = "out\$Target"

  # What the machine was already offering, before any of it is taken away.
  Write-Host "==> INCLUDE was: $(if ($env:INCLUDE) { $env:INCLUDE } else { '(unset)' })"
  Write-Host "==> LIB was:     $(if ($env:LIB) { $env:LIB } else { '(unset)' })"

  # Clearing INCLUDE is the same idea as hiding the checkout. clang-cl reads it
  # for system headers, and a runner that already exports a Visual Studio
  # INCLUDE would compile this bundle whether or not setup.ps1 repointed a
  # single -imsvc path - green for a reason the bundle had nothing to do with.
  # LIB stays: the LINK needs Microsoft's import libraries by bare name, and
  # nothing in the bundle claims to provide those.
  $env:INCLUDE = ''
  if (-not $env:LIB) {
    # No developer environment here. Build the same list a developer prompt
    # would set, so the link failing means the link is wrong rather than that
    # this happened to run in a bare shell.
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path $vswhere) {
      $vs = & $vswhere -latest -products * -property installationPath 2>$null | Select-Object -First 1
      $tools = $null
      if ($vs) {
        $tools = Get-ChildItem (Join-Path $vs 'VC\Tools\MSVC') -Directory -ErrorAction SilentlyContinue |
          Sort-Object Name -Descending | Select-Object -First 1
      }
      $kits = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots' -ErrorAction SilentlyContinue).KitsRoot10
      # The ARCHITECTURE OF THE BUNDLE, not of whatever built this script last.
      # These paths were a hardcoded x64, so verifying an arm64 bundle pointed
      # the linker at x64 import libraries and every single libc++ object came
      # back "machine type arm64 conflicts with x64" -- a wall of errors that
      # reads like a broken bundle and is really a wrong -Target lookup.
      $arch = if ($Target -like '*-arm64') { 'arm64' } else { 'x64' }
      $libs = @()
      if ($tools) { $libs += (Join-Path $tools.FullName "lib\$arch") }
      if ($kits) {
        $sdk = Get-ChildItem (Join-Path $kits 'Lib') -Directory -ErrorAction SilentlyContinue |
          Where-Object { $_.Name -like '10.*' } | Sort-Object Name -Descending | Select-Object -First 1
        if ($sdk) {
          foreach ($part in @("um\$arch", "ucrt\$arch")) {
            $p = Join-Path $sdk.FullName $part
            if (Test-Path $p) { $libs += $p }
          }
        }
      }
      if ($libs) {
        $env:LIB = $libs -join ';'
        Write-Host "==> LIB set to: $env:LIB"
      }
    }
  }

  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  & $builder -WebrtcSrc $env:WEBRTC_SRC -WebrtcOut $env:WEBRTC_OUT -Dest $Dest
  if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "the wrapper build exited $LASTEXITCODE" }
  $sw.Stop()
  $elapsed = [int]$sw.Elapsed.TotalSeconds

  $dll = Join-Path $Dest 'veil_media.dll'
  if (-not (Test-Path $dll)) {
    throw "the build reported success and produced no $dll"
  }

  # Read the export table. dumpbin is the native reader and is what xVeil's own
  # Windows symbol gate uses; llvm-objdump out of the BUNDLE'S OWN toolchain is
  # the fallback, so a machine without Visual Studio's tools can still run this.
  $names = $null
  $dumpbin = Get-ChildItem -Recurse -Filter dumpbin.exe `
    -Path "${env:ProgramFiles(x86)}\Microsoft Visual Studio", "$env:ProgramFiles\Microsoft Visual Studio" `
    -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($dumpbin) {
    $reader = "dumpbin $($dumpbin.FullName)"
    $names = & $dumpbin.FullName /EXPORTS $dll |
      Select-String -Pattern '\bveil_media_[a-z0-9_]+' -AllMatches -CaseSensitive |
      ForEach-Object { $_.Matches.Value } | Sort-Object -Unique
  } else {
    $objdump = Join-Path $env:WEBRTC_SRC 'third_party\llvm-build\Release+Asserts\bin\llvm-objdump.exe'
    if (-not (Test-Path $objdump)) { throw 'neither dumpbin.exe nor the bundle llvm-objdump.exe can be found - cannot read an export table' }
    $reader = "llvm-objdump $objdump"
    $names = & $objdump -p $dll |
      Select-String -Pattern '\bveil_media_[a-z0-9_]+' -AllMatches -CaseSensitive |
      ForEach-Object { $_.Matches.Value } | Sort-Object -Unique
  }

  $count = @($names).Count
  $bytes = (Get-Item $dll).Length
  Write-Host "==> PE $dll (read with $reader)"
  Write-Host "==> $bytes bytes, $count exported veil_media_* symbols, built in ${elapsed}s"

  # 87 today. A floor, not an equality: adding an entry point to the ABI must
  # not turn this red, and dropping half of them must not stay green.
  $FLOOR = 80
  if ($count -lt $FLOOR) {
    Write-Host "::error::only $count veil_media_* symbols exported (floor $FLOOR) - the bundle is incomplete or the engine compiled as its stub"
    exit 1
  }
  Write-Host "==> VERIFIED: the bundle alone builds the wrapper in ${elapsed}s"
} finally {
  if ($hidden -and (Test-Path $hidden)) {
    Move-Item -LiteralPath $hidden -Destination $Hide
    Write-Host "restored $Hide"
  }
}
