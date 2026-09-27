[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DeploymentUuid,
    [string]$ApplicationUuid
)

$ErrorActionPreference = 'Stop'
foreach ($name in @('COOLIFY_URL', 'COOLIFY_API_TOKEN')) {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "Variable requise absente : $name"
    }
}

$deployment = Invoke-RestMethod -Method Get `
    -Uri "$($env:COOLIFY_URL.TrimEnd('/'))/api/v1/deployments/$DeploymentUuid" `
    -Headers @{ Authorization = "Bearer $env:COOLIFY_API_TOKEN" }

if ($null -eq $deployment.logs -and $ApplicationUuid) {
    $history = Invoke-RestMethod -Method Get `
        -Uri "$($env:COOLIFY_URL.TrimEnd('/'))/api/v1/deployments/applications/$ApplicationUuid" `
        -Headers @{ Authorization = "Bearer $env:COOLIFY_API_TOKEN" }
    $candidates = if ($history.deployments) { $history.deployments } else { $history }
    foreach ($candidate in $candidates) {
        if ($candidate.deployment_uuid -eq $DeploymentUuid) {
            $deployment | Add-Member -NotePropertyName logs -NotePropertyValue $candidate.logs -Force
            break
        }
    }
}

$safeErrorLines = @()
foreach ($entry in @($deployment.logs)) {
    $line = if ($entry -is [string]) { $entry } elseif ($entry.output) { [string]$entry.output } else { '' }
    if ($line -match '(?i)(error|failed|failure|cannot|unable|invalid|exit code|exception)') {
        $line = $line -replace '(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+', '$1<redacted>'
        $line = $line -replace '[A-Fa-f0-9]{64}', '<sha256>'
        $line = $line -replace '[A-Za-z0-9_-]{48,}', '<redacted>'
        if ($line.Length -gt 500) { $line = $line.Substring(0, 500) }
        $safeErrorLines += $line
    }
}

[pscustomobject]@{
    deploymentUuid = $deployment.deployment_uuid
    status = $deployment.status
    applicationUuid = $deployment.application_uuid
    applicationName = $deployment.application_name
    commit = $deployment.commit
    message = $deployment.message
    createdAt = $deployment.created_at
    finishedAt = $deployment.finished_at
    logType = if ($null -eq $deployment.logs) { 'null' } else { $deployment.logs.GetType().FullName }
    logCount = @($deployment.logs).Count
    properties = @($deployment.PSObject.Properties.Name)
    errorLines = @($safeErrorLines | Select-Object -Last 20)
} | ConvertTo-Json -Compress
