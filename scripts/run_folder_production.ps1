$ErrorActionPreference = 'Stop'

$sourceRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = $env:VIDEO_FACTORY_PROJECT_ROOT
if ([string]::IsNullOrWhiteSpace($projectRoot)) {
  $projectRoot = if (Test-Path -LiteralPath 'D:\人物一致性') { 'D:\人物一致性' } else { $sourceRoot }
}
$scriptRoot = $env:VIDEO_FACTORY_SOURCE_ROOT
if ([string]::IsNullOrWhiteSpace($scriptRoot)) { $scriptRoot = 'D:\脚本' }
$python = $env:VIDEO_FACTORY_PYTHON
if ([string]::IsNullOrWhiteSpace($python)) { $python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
$runner = Join-Path $PSScriptRoot 'one_click_folder_production.py'
$stateFile = $env:VIDEO_FACTORY_STATE_FILE
$runtimeDir = $env:VIDEO_FACTORY_RUNTIME_DIR
if ([string]::IsNullOrWhiteSpace($runtimeDir)) { $runtimeDir = Join-Path $projectRoot '.local-runtime' }
if ([string]::IsNullOrWhiteSpace($stateFile)) { $stateFile = Join-Path $runtimeDir 'folder-production-state.json' }
$apiFile = Join-Path $runtimeDir 'folder-production-api.txt'
$sshFile = $env:VIDEO_FACTORY_SSH_CONFIG
if ([string]::IsNullOrWhiteSpace($sshFile)) { $sshFile = Join-Path $runtimeDir 'folder-production-ssh.txt' }
$knownHosts = $env:VIDEO_FACTORY_SSH_KNOWN_HOSTS
if ([string]::IsNullOrWhiteSpace($knownHosts)) { $knownHosts = Join-Path $runtimeDir 'vast-known-hosts' }
$manifestDir = $env:VIDEO_FACTORY_MANIFEST_DIR
if ([string]::IsNullOrWhiteSpace($manifestDir)) { $manifestDir = Join-Path $runtimeDir 'batch_40_production' }
$expectedCount = 0
if (-not [string]::IsNullOrWhiteSpace($env:VIDEO_FACTORY_EXPECTED_COUNT) -and
    -not [int]::TryParse($env:VIDEO_FACTORY_EXPECTED_COUNT, [ref]$expectedCount)) {
  throw "VIDEO_FACTORY_EXPECTED_COUNT 不是整数：$env:VIDEO_FACTORY_EXPECTED_COUNT"
}
$sshOut = Join-Path $runtimeDir 'folder-production-ssh.stdout.log'
$sshErr = Join-Path $runtimeDir 'folder-production-ssh.stderr.log'
$sshProcess = $null

function Test-ApiHealth([string]$BaseUrl) {
  try {
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/health" -Method Get -TimeoutSec 2
    return $response.status -eq 'ok'
  } catch {
    return $false
  }
}

try {
  if (-not (Test-Path -LiteralPath $python)) { throw "项目 Python 不存在：$python" }
  if (-not (Test-Path -LiteralPath $runner)) { throw "批量制作程序不存在：$runner" }
  if (-not (Test-Path -LiteralPath $scriptRoot)) { throw "脚本目录不存在：$scriptRoot" }

  $apiUrl = $env:VIDEO_FACTORY_API_URL
  if ([string]::IsNullOrWhiteSpace($apiUrl) -and (Test-Path -LiteralPath $sshFile)) {
    $lines = @(Get-Content -LiteralPath $sshFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
    if ($lines.Count -lt 2) { throw "SSH 配置格式无效：$sshFile 需要两行，分别是 root@HOST 与 SSH_PORT。" }
    $target = $lines[0]
    $sshPort = 0
    if ($target -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$' -or -not [int]::TryParse($lines[1], [ref]$sshPort) -or $sshPort -lt 1 -or $sshPort -gt 65535) {
      throw "SSH 配置无效：第一行应为 root@HOST，第二行为 1–65535 的 SSH 端口。"
    }

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $localPort = $listener.LocalEndpoint.Port
    $listener.Stop()
    $apiUrl = "http://127.0.0.1:$localPort"
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    if (-not (Test-Path -LiteralPath $knownHosts)) { throw "没有固定的 SSH 主机密钥文件：$knownHosts；拒绝建立未验证主机的隧道。" }
    $sshArgs = @(
      '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
      '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
      '-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$knownHosts",
      '-L', "127.0.0.1:${localPort}:127.0.0.1:18766",
      '-p', "$sshPort", $target
    )
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    Remove-Item -LiteralPath $sshOut, $sshErr -Force -ErrorAction SilentlyContinue
    $sshProcess = Start-Process -FilePath $ssh -ArgumentList $sshArgs -PassThru -WindowStyle Hidden `
      -RedirectStandardOutput $sshOut -RedirectStandardError $sshErr

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
      if ($sshProcess.HasExited) {
        $details = if (Test-Path -LiteralPath $sshErr) { (Get-Content -LiteralPath $sshErr -Tail 20) -join "`n" } else { 'SSH 子进程提前退出。' }
        throw "无法建立到实例的 SSH 隧道。请检查本机 SSH 密钥、主机与端口。`n$details"
      }
      if (Test-ApiHealth $apiUrl) { break }
      Start-Sleep -Milliseconds 500
    }
    if (-not (Test-ApiHealth $apiUrl)) { throw "SSH 隧道已启动，但实例自动化 API 在 30 秒内未就绪：$apiUrl/api/health" }
    Write-Host "已通过仅本机可访问的 SSH 隧道连接 AI_Video_automation（远端 127.0.0.1:18766）。"
  } elseif ([string]::IsNullOrWhiteSpace($apiUrl) -and (Test-Path -LiteralPath $apiFile)) {
    $apiUrl = (Get-Content -LiteralPath $apiFile -Raw).Trim()
  }

  if ([string]::IsNullOrWhiteSpace($apiUrl)) { $apiUrl = 'http://127.0.0.1:8765' }
  $apiUrl = $apiUrl.TrimEnd('/')
  $env:VIDEO_FACTORY_API_URL = $apiUrl

  Write-Host ''
  Write-Host '扫描 Word、绑定同目录共享产品图并执行只读预检。'
  Write-Host "Word 根目录：$scriptRoot"
  Write-Host "应用 API：$apiUrl"
  Write-Host '默认工作流：H3 SMD4 Fast 4-step + 本机已验证 Real-ESRGAN 4K 超分（型号按画幅选择）。'
  Write-Host '云端服务与付费回退：关闭。'
  New-Item -ItemType Directory -Path $manifestDir -Force | Out-Null
  $dryRunOutput = & $python $runner --root $scriptRoot --api $apiUrl --all --dry-run --limit 500 --state-file $stateFile 2>&1
  $dryRunExit = $LASTEXITCODE
  $dryRunText = ($dryRunOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
  if ($dryRunExit -ne 0) { throw "扫描或预检失败（exit=$dryRunExit）；没有提交生成任务。`n$dryRunText" }
  $jsonMatch = [regex]::Match($dryRunText, '(?s)(\{.*\})')
  if (-not $jsonMatch.Success) { throw "预检没有返回 JSON 计划；没有提交生成任务。`n$dryRunText" }
  try { $plan = $jsonMatch.Groups[1].Value | ConvertFrom-Json -ErrorAction Stop }
  catch { throw "预检结果不是有效 JSON；没有提交生成任务。$($_.Exception.Message)" }
  if ($expectedCount -gt 0 -and $plan.selected_count -ne $expectedCount) {
    throw "预期 $expectedCount 份脚本，实际扫描到 $($plan.selected_count) 份；没有提交生成任务。"
  }
  $badAssets = @($plan.plans | Where-Object { $_.product_match -ne $true -or $_.product_images.Count -lt 1 })
  $badQuality = @($plan.plans | Where-Object {
    $_.quality.mode -ne 'fast' -or $_.quality.resolution -ne '4k' -or
    $_.quality.processing -ne 'ai_super_resolution' -or $_.quality.aspect_ratio -notin @('16:9', '9:16', '1:1')
  })
  $blocked = @($plan.plans | Where-Object { $_.blockers -and $_.blockers.Count -gt 0 })
  $notReady = @($plan.plans | Where-Object {
    $_.generation_preflight.ready -ne $true -or $_.quality_preflight.supported -ne $true
  })
  if ($blocked.Count -gt 0 -or $notReady.Count -gt 0 -or $badAssets.Count -gt 0 -or $badQuality.Count -gt 0) {
    $bad = @($blocked + $notReady + $badAssets + $badQuality | Select-Object -ExpandProperty script -Unique)
    throw "有 $($bad.Count) 份脚本未通过全部只读预检；没有提交生成任务。`n$($bad -join "`n")"
  }
  $assetManifest = [ordered]@{
    version = 1
    generated_at = [DateTime]::UtcNow.ToString('o')
    source_root = $scriptRoot
    scripts = @($plan.plans | ForEach-Object {
      [ordered]@{
        script_id = $_.script_sha256.Substring(0, 16)
        original_path = $_.script
        document_sha256 = $_.script_sha256
        product_group = $_.product_group
        product_name = $_.product_name
        brand = $_.brand
        product_images = $_.product_images
      }
    })
  }
  $batchManifest = [ordered]@{
    version = 1
    batch_id = "batch40_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    created_at = [DateTime]::UtcNow.ToString('o')
    status = 'PREFLIGHT_PASSED_NOT_SUBMITTED'
    expected_count = $plan.selected_count
    source_root = $scriptRoot
    workflow = [ordered]@{
      route = 'local Context-IR'
      model = 'MiniMax H3'
      workflow_name = 'H3 SMD4 Fast'
      mode = 'fast'
      steps = 4
      resolution = '4k'
      processing = 'ai_super_resolution'
      aspect_ratio_policy = 'read from each Word document'
      cloud_fallback = $false
      upscaler = 'Real-ESRGAN; selected per aspect ratio from verified capability'
    }
    items = @($plan.plans | ForEach-Object {
      [ordered]@{
        task_id = $_.script_sha256.Substring(0, 16)
        script = $_.script
        script_sha256 = $_.script_sha256
        image_sha256 = @($_.product_images | ForEach-Object { $_.sha256 })
        quality = $_.quality
        generation_preflight = $_.generation_preflight
        quality_preflight = $_.quality_preflight
        status = 'READY'
      }
    })
  }
  $utf8 = [System.Text.UTF8Encoding]::new($false)
  $assetManifestPath = Join-Path $manifestDir 'script_asset_manifest.json'
  $batchManifestPath = Join-Path $manifestDir 'batch_40_manifest.json'
  [System.IO.File]::WriteAllText($assetManifestPath, ($assetManifest | ConvertTo-Json -Depth 40), $utf8)
  [System.IO.File]::WriteAllText($batchManifestPath, ($batchManifest | ConvertTo-Json -Depth 60), $utf8)
  Write-Host "只读预检通过：$($plan.selected_count) 份脚本、全部 Context-IR 与超分能力已验证。"
  Write-Host "脚本与产品图映射：$assetManifestPath"
  Write-Host "可恢复批次台账：$batchManifestPath"

  Write-Host ''
  $confirm = Read-Host '复核脚本、产品图哈希与服务能力后，输入 START ALL 开始批量生成'
  if ($confirm -cne 'START ALL') { Write-Host '已取消；没有提交生成任务。'; exit 0 }

  $batchManifest.status = 'SUBMITTING'
  $batchManifest.submission = [ordered]@{ status = 'SUBMITTING'; started_at = [DateTime]::UtcNow.ToString('o') }
  [System.IO.File]::WriteAllText($batchManifestPath, ($batchManifest | ConvertTo-Json -Depth 60), $utf8)
  & $python $runner --root $scriptRoot --api $apiUrl --all --submit --confirm-submit --wait --continue-on-error --limit 500 --state-file $stateFile
  $submitExit = $LASTEXITCODE
  $batchManifest.status = if ($submitExit -eq 0) { 'SUBMISSION_PROCESS_COMPLETED_VERIFY_LEDGER' } else { 'SUBMISSION_PROCESS_FAILED_REVIEW_LEDGER' }
  $batchManifest.submission = [ordered]@{ status = $batchManifest.status; exit_code = $submitExit; finished_at = [DateTime]::UtcNow.ToString('o') }
  [System.IO.File]::WriteAllText($batchManifestPath, ($batchManifest | ConvertTo-Json -Depth 60), $utf8)
  if ($submitExit -ne 0) { throw "批量处理有失败或阻断项（exit=$submitExit）；状态已写入台账。" }
  Write-Host '批量任务已处理完成；请在 AI_Video_automation 中查看成片和异常项。'
  exit 0
} catch {
  Write-Error $_
  exit 1
} finally {
  if ($sshProcess -and -not $sshProcess.HasExited) {
    Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue
  }
}
