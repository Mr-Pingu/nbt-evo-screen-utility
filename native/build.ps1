param(
    [string]$SdkPath = $env:QNX_SDK_PATH
)

$ErrorActionPreference = 'Stop'
if (-not $SdkPath) {
    throw 'Set QNX_SDK_PATH to the QNX 6.5 SDK root or pass -SdkPath.'
}
$previousLocation = Get-Location
$previousHost = $env:QNX_HOST
$previousTarget = $env:QNX_TARGET
$previousPath = $env:Path
$createdDrive = $null

try {
    $resolvedSdk = (Resolve-Path -LiteralPath $SdkPath).ProviderPath.TrimEnd('\')
    $sdkRoot = $resolvedSdk
    # This older SDK cannot reliably use an installation path containing spaces.
    if ($resolvedSdk.Contains(' ')) {
        $existingMapping = subst.exe | Where-Object { $_ -like "*=> $resolvedSdk" } | Select-Object -First 1
        if ($existingMapping) {
            $sdkRoot = $existingMapping.Substring(0, 2)
        } else {
            $availableDrive = 90..80 | ForEach-Object { "$([char]$_):" } |
                Where-Object { -not (Get-PSDrive -Name $_.Substring(0, 1) -ErrorAction SilentlyContinue) } |
                Select-Object -First 1
            if (-not $availableDrive) { throw 'No free drive letter for the QNX SDK.' }
            subst.exe $availableDrive $resolvedSdk
            if ($LASTEXITCODE -ne 0) { throw 'Could not map the QNX SDK.' }
            $createdDrive = $availableDrive
            $sdkRoot = $availableDrive
        }
    }
    $env:QNX_HOST = "$sdkRoot\host\win32\x86"
    $env:QNX_TARGET = "$sdkRoot\target\qnx6"
    $env:Path = "$env:QNX_HOST\usr\bin;$previousPath"
    $compiler = "$env:QNX_HOST\usr\bin\ntoarmv7-gcc.exe"
    if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        throw 'QNX 6.5 ARMv7 compiler not found. Supply its SDK root with -SdkPath.'
    }
    Set-Location -LiteralPath $PSScriptRoot
    $compileArgs = @('-D_QNX_SOURCE', '-std=c99', '-O2', '-Wall', '-Wextra',
        '-march=armv7-a', '-mfloat-abi=softfp', '-mfpu=neon', '-lsocket')
    & $compiler @compileArgs 'hu_screen_mirror.c' -o 'hu-screen-mirror'
    if ($LASTEXITCODE -ne 0) { throw 'Build failed: hu_screen_mirror.c' }
    Write-Host 'Built hu-screen-mirror'
} finally {
    Set-Location -LiteralPath $previousLocation.Path
    $env:QNX_HOST = $previousHost
    $env:QNX_TARGET = $previousTarget
    $env:Path = $previousPath
    if ($createdDrive) { subst.exe $createdDrive /D }
}
