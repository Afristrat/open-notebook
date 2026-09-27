[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ApplicationUuid,
    [Parameter(Mandatory = $true)][string]$Key
)

$ErrorActionPreference = 'Stop'
foreach ($name in @('COOLIFY_URL', 'COOLIFY_API_TOKEN')) {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "Variable requise absente : $name"
    }
}

$response = Invoke-RestMethod -Method Get `
    -Uri "$($env:COOLIFY_URL.TrimEnd('/'))/api/v1/applications/$ApplicationUuid/envs" `
    -Headers @{ Authorization = "Bearer $env:COOLIFY_API_TOKEN" }
$items = @()
foreach ($item in $response) { $items += $item }

@($items | Where-Object { $_.key -eq $Key } | ForEach-Object {
        [pscustomobject]@{
            uuid = $_.uuid
            key = $_.key
            isPreview = $_.is_preview
            isLiteral = $_.is_literal
            isBuildtime = $_.is_buildtime
        }
    }) | ConvertTo-Json -Compress
