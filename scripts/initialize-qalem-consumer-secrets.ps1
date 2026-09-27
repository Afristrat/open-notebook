[CmdletBinding()]
param(
    [string]$QalemOrganizationId = 'aa7870b7-3938-4f24-b8bf-4a9d73565ba7'
)

$ErrorActionPreference = 'Stop'

function New-ServiceToken {
    $bytes = [byte[]]::new(48)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    }
    finally {
        $generator.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash($bytes)
        return ([BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

$token = New-ServiceToken
$originalModulePath = $env:PSModulePath
try {
    $map = @{ $QalemOrganizationId = $token } | ConvertTo-Json -Compress
    $digest = Get-Sha256 -Value $token
    $diwanConfig = "qalem:$QalemOrganizationId`:$digest"

    # add-secret revalide dans Windows PowerShell 5.1. Le runtime Codex ajoute
    # des modules PowerShell 7 en tete de PSModulePath ; l'enfant 5.1 trouve
    # alors un module Security incompatible. Lui transmettre uniquement ses
    # chemins natifs pendant la transaction maintient la sonde officielle.
    $env:PSModulePath = @(
        "$env:ProgramFiles\WindowsPowerShell\Modules"
        "$env:windir\System32\WindowsPowerShell\v1.0\Modules"
    ) -join ';'
    & 'C:\Users\amans\.claude\scripts\add-secret.ps1' `
        -Name 'QALEM_DIWAN_TENANT_TOKENS' -Value $map | Out-Null
    & 'C:\Users\amans\.claude\scripts\add-secret.ps1' `
        -Name 'DIWAN_CONSUMER_TOKENS' -Value $diwanConfig | Out-Null

    [pscustomobject]@{
        qalemSecret = 'QALEM_DIWAN_TENANT_TOKENS'
        diwanSecret = 'DIWAN_CONSUMER_TOKENS'
        organizationId = $QalemOrganizationId
        tokenLength = $token.Length
        digestLength = $digest.Length
    } | ConvertTo-Json -Compress
}
finally {
    $env:PSModulePath = $originalModulePath
    $token = $null
    $map = $null
    $diwanConfig = $null
}
