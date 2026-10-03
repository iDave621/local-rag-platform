[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'start', 'status', 'stop')]
    [string]$Action = 'start',
    [string[]]$ServerName = @()
)

$ErrorActionPreference = 'Stop'
$packageRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$packageNative = $packageRoot.Replace('\', '/')
$appRoot = Join-Path $packageRoot 'app'
$runtimeRoot = Join-Path $packageRoot 'runtime'
$installMarker = Join-Path $runtimeRoot 'install.json'
$images = @(
    'local-rag/backend:2.2.0', 'local-rag/frontend:2.2.0',
    'local-rag/postgres:2.2.0', 'local-rag/redis:2.2.0',
    'local-rag/chroma:2.2.0', 'local-rag/ollama:2.2.0',
    'local-rag/gateway:2.2.0'
)

function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed with exit code $LASTEXITCODE." }
}

function Test-PackageIntegrity {
    $manifest = Join-Path $packageRoot 'SHA256SUMS'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw 'SHA256SUMS is missing. Extract the complete standalone server package first.'
    }
    Write-Host 'Verifying packaged code, images, and models. This can take a few minutes.'
    $checked = 0
    $required = @{'payload/images.tar.gz' = $false; 'payload/models.tar.gz' = $false; 'setup.py' = $false}
    foreach ($line in [System.IO.File]::ReadAllLines($manifest)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^([a-fA-F0-9]{64})\s+[*]?(.+)$') { throw 'Invalid SHA256SUMS entry.' }
        $expected = $Matches[1]
        $relative = $Matches[2]
        $path = [System.IO.Path]::GetFullPath((Join-Path $packageRoot $relative))
        $prefix = $packageRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
        if (-not $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'A package checksum path points outside the extracted package.'
        }
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing package file: $relative" }
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if ($actual -ne $expected) { throw "Package checksum mismatch: $relative" }
        $normalized = $relative.Replace('\', '/').TrimStart('.', '/')
        if ($required.ContainsKey($normalized)) { $required[$normalized] = $true }
        $checked++
    }
    if ($checked -eq 0 -or $required.Values -contains $false) {
        throw 'The checksum manifest must cover setup.py and both image/model archives.'
    }
    Write-Host "Verified $checked package files."
}

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker is required. Install/start Docker Desktop and choose Linux containers.'
    }
    $dockerInfo = (Invoke-Docker -Arguments @('info', '--format', '{{.OSType}} {{.Architecture}}')) -join ' '
    $platform = $dockerInfo.Trim() -split '\s+'
    if ($platform.Length -ne 2 -or $platform[0] -ne 'linux' -or $platform[1] -notin @('amd64', 'x86_64')) {
        throw 'This package requires a Linux x86_64 Docker engine. Switch Docker Desktop to Linux containers.'
    }
    $composeOutput = (Invoke-Docker -Arguments @('compose', 'version', '--short')) -join ''
    if ($composeOutput.Trim() -notmatch '^v?(\d+)\.(\d+)\.(\d+)') { throw 'Cannot determine Docker Compose version.' }
    $composeVersion = [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3])
    if ($composeVersion -lt [version]'2.24.4') { throw 'Docker Compose 2.24.4 or newer is required.' }

    if ($Action -in @('status', 'stop')) {
        if (-not (Test-Path -LiteralPath $installMarker -PathType Leaf)) {
            Write-Host 'This package has not been installed. Run RUN.cmd start.'
            exit 0
        }
        Push-Location $appRoot
        try {
            if ($Action -eq 'status') { Invoke-Docker -Arguments @('compose', 'ps', '-a') }
            else { Invoke-Docker -Arguments @('compose', 'stop') }
        } finally { Pop-Location }
        exit 0
    }

    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot 'setup.py') -PathType Leaf)) {
        throw 'setup.py is missing. Run this launcher from the extracted standalone server package.'
    }
    $mustLoad = -not (Test-Path -LiteralPath $installMarker -PathType Leaf)
    if (-not $mustLoad) {
        & docker image inspect @images *> $null
        $mustLoad = $LASTEXITCODE -ne 0
    }
    if ($mustLoad) {
        Test-PackageIntegrity
        Write-Host 'Loading the packaged Docker images locally.'
        Invoke-Docker -Arguments @('load', '--input', (Join-Path $packageRoot 'payload/images.tar.gz'))
    }
    [System.IO.Directory]::CreateDirectory($runtimeRoot) | Out-Null
    $inspection = Invoke-Docker -Arguments (@('image', 'inspect') + $images)
    [System.IO.File]::WriteAllText((Join-Path $runtimeRoot 'image-inspection.json'), ($inspection -join "`n"), [System.Text.UTF8Encoding]::new($false))
    $names = @('localhost', [Environment]::MachineName) + $ServerName
    if (Get-Command Get-NetIPAddress -ErrorAction SilentlyContinue) {
        try {
            $names += Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
                Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -ne '0.0.0.0' -and -not $_.IPAddress.StartsWith('169.254.') } |
                Select-Object -ExpandProperty IPAddress
        } catch { Write-Host 'Automatic LAN address detection unavailable; configure server_names in run-config.json if needed.' }
    }
    $helper = @('run', '--rm', '--pull', 'never', '--network', 'none', '--mount', "type=bind,source=$packageNative,target=/package", '--entrypoint', 'python', 'local-rag/backend:2.2.0', '/package/setup.py', 'prepare', '--host-root', $packageNative)
    foreach ($name in ($names | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)) {
        $helper += @('--server-name', $name)
    }
    Invoke-Docker -Arguments $helper
    Push-Location $appRoot
    try {
        Write-Host 'Starting the offline server and waiting for its health checks.'
        Invoke-Docker -Arguments @('compose', 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '600')
        Invoke-Docker -Arguments @('compose', 'run', '--rm', '--no-deps', '-T', '-v', "${packageNative}:/package", 'api', 'python', '/package/setup.py', 'provision')
    } finally { Pop-Location }
    Invoke-Docker -Arguments @('run', '--rm', '--pull', 'never', '--network', 'none', '--mount', "type=bind,source=$packageNative,target=/package", '--entrypoint', 'python', 'local-rag/backend:2.2.0', '/package/setup.py', 'show')
    Write-Host 'The server is ready. Use RUN.cmd status to inspect it or RUN.cmd stop to stop it.'
} catch {
    [Console]::Error.WriteLine("Server setup failed: $($_.Exception.Message)")
    exit 1
}
