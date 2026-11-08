$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Get-Dependency($Url, $Destination) {
    Write-Host "Downloading $Url"
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination
}

function Invoke-Python($Python, [string[]]$Arguments) {
    & $Python -B -X utf8 @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python exited with code $LASTEXITCODE."
    }
}

function Test-Runtime($Python) {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        return $false
    }
    $ErrorActionPreference = 'Continue'
    & $Python -B -c 'import cv2, torch, torchvision, spandrel' *> $null
    return $LASTEXITCODE -eq 0
}

$source = $PSScriptRoot
$tempRoot = (Get-Item -LiteralPath ([IO.Path]::GetTempPath())).FullName.TrimEnd('\')
$prepared = Join-Path $tempRoot ('video-upscale-deps-' + [guid]::NewGuid().ToString('N'))
$status = 0

try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'VideoUpscale4k requires 64-bit Windows.'
    }
    foreach ($file in @('install.py', 'menu.py', 'worker.py', 'nuke_video_upscale_node.py', 'app/video_upscale_core.py', 'README.md')) {
        if (-not (Test-Path -LiteralPath (Join-Path $source $file) -PathType Leaf)) {
            throw "Source package is incomplete: $file"
        }
    }
    New-Item -ItemType Directory -Path $prepared | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $python = Join-Path $source 'runtime/python.exe'
    if (-not (Test-Runtime $python)) {
        $runtime = Join-Path $prepared 'runtime'
        $pythonZip = Join-Path $prepared 'python.zip'
        Get-Dependency 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' $pythonZip
        Expand-Archive -LiteralPath $pythonZip -DestinationPath $runtime
        $python = Join-Path $runtime 'python.exe'
        $packages = Join-Path $runtime 'Lib/site-packages'
        New-Item -ItemType Directory -Path $packages -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $runtime 'python312._pth') -Encoding ASCII -Value @(
            'python312.zip', '.', 'Lib\site-packages'
        )
        $getPip = Join-Path $prepared 'get-pip.py'
        Get-Dependency 'https://bootstrap.pypa.io/get-pip.py' $getPip
        Invoke-Python $python @($getPip, '--no-setuptools', '--no-wheel', '--no-cache-dir', '--target', $packages)

        Write-Host 'Installing PyTorch CUDA 12.8 and image processing packages. This may take several minutes.'
        $wheels = 'https://download-r2.pytorch.org/whl/cu128'
        Invoke-Python $python @(
            '-m', 'pip', '--isolated', 'install', '--no-cache-dir', '--only-binary=:all:',
            '--index-url', 'https://pypi.org/simple', '--target', $packages,
            "$wheels/torch-2.9.1%2Bcu128-cp312-cp312-win_amd64.whl",
            "$wheels/torchvision-0.24.1%2Bcu128-cp312-cp312-win_amd64.whl",
            'numpy==1.26.4', 'opencv-python==4.10.0.84', 'spandrel==0.4.2'
        )
        Invoke-Python $python @('-c', "import cv2, torch, torchvision, spandrel; assert torch.version.cuda == '12.8'")
    }

    if (-not ((Test-Path -LiteralPath (Join-Path $source 'bin/ffmpeg.exe') -PathType Leaf) -and
              (Test-Path -LiteralPath (Join-Path $source 'bin/ffprobe.exe') -PathType Leaf))) {
        $ffmpegZip = Join-Path $prepared 'ffmpeg.zip'
        $ffmpegRoot = Join-Path $prepared 'ffmpeg'
        $bin = Join-Path $prepared 'bin'
        Get-Dependency 'https://github.com/GyanD/codexffmpeg/releases/download/7.1.1/ffmpeg-7.1.1-essentials_build.zip' $ffmpegZip
        Expand-Archive -LiteralPath $ffmpegZip -DestinationPath $ffmpegRoot
        $build = Join-Path $ffmpegRoot 'ffmpeg-7.1.1-essentials_build'
        New-Item -ItemType Directory -Path $bin | Out-Null
        Copy-Item -LiteralPath @((Join-Path $build 'bin/ffmpeg.exe'), (Join-Path $build 'bin/ffprobe.exe')) -Destination $bin
        Copy-Item -LiteralPath (Join-Path $build 'LICENSE') -Destination (Join-Path $bin 'FFmpeg-LICENSE.txt')
    }

    $model = 'models/upscale/RealESRGAN_x4plus.pth'
    if (-not (Test-Path -LiteralPath (Join-Path $source $model) -PathType Leaf)) {
        New-Item -ItemType Directory -Path (Join-Path $prepared 'models/upscale') -Force | Out-Null
        Get-Dependency 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth' (Join-Path $prepared $model)
    }

    Invoke-Python $python @((Join-Path $source 'install.py'), '--prepared', $prepared)
} catch {
    [Console]::Error.WriteLine("Installation failed: $($_.Exception.Message)")
    $status = 1
} finally {
    if (Test-Path -LiteralPath $prepared -PathType Container) {
        $resolved = (Resolve-Path -LiteralPath $prepared).ProviderPath
        if ($resolved -eq $prepared -and (Split-Path -Parent $resolved) -eq $tempRoot) {
            Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
exit $status
