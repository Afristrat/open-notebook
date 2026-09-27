[CmdletBinding()]
param(
    [string]$DiwanApplicationUuid = 'ohir87jvt32284sh6wfwhhz2',
    [string]$QalemApplicationUuid = 'bcx5pxyuc9z3lt4jtyjipcqu',
    [string]$QalemOrganizationId = 'aa7870b7-3938-4f24-b8bf-4a9d73565ba7',
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$DiwanCommit,
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$QalemCommit = 'd1febcdade06852b5323b89ce9857bbc3349ebc7'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

foreach ($name in @(
        'COOLIFY_URL',
        'COOLIFY_API_TOKEN',
        'QALEM_DIWAN_TENANT_TOKENS',
        'DIWAN_CONSUMER_TOKENS'
    )) {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "Variable requise absente : $name"
    }
}

$coolifyBase = $env:COOLIFY_URL.TrimEnd('/')
$coolifyHeaders = @{ Authorization = "Bearer $env:COOLIFY_API_TOKEN" }
$diwanBase = 'https://diwan.ai-mpower.com/api/v1/consumers/qalem'
$secondaryOrganizationId = [guid]::NewGuid().ToString()

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

function Invoke-SafeRest {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Get', 'Post', 'Patch', 'Delete')][string]$Method,
        [Parameter(Mandatory = $true)][string]$Uri,
        [hashtable]$Headers = @{},
        [object]$Body,
        [hashtable]$Form,
        [Parameter(Mandatory = $true)][string]$Label
    )

    try {
        $parameters = @{ Method = $Method; Uri = $Uri; Headers = $Headers; TimeoutSec = 30 }
        if ($null -ne $Body) {
            $parameters.Body = $Body | ConvertTo-Json -Depth 12 -Compress
            $parameters.ContentType = 'application/json'
        }
        elseif ($null -ne $Form) {
            $parameters.Form = $Form
        }
        return Invoke-RestMethod @parameters
    }
    catch {
        $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        throw "$Label : HTTP $status"
    }
}

function Get-HttpStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [hashtable]$Headers = @{}
    )
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -Headers $Headers -TimeoutSec 30 | Out-Null
        return 200
    }
    catch {
        return if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
    }
}

function Set-CoolifyEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)][string]$ApplicationUuid,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value,
        [switch]$Blind
    )

    if ($Blind) {
        # Certaines anciennes applications Coolify renvoient 500 sur GET /envs.
        # Un POST minimal cree runtime + preview ; en cas de doublon, le PATCH
        # par cle et drapeau preview reste l'upsert officiel deja utilise par Qalem.
        try {
            Invoke-RestMethod -Method Post `
                -Uri "$coolifyBase/api/v1/applications/$ApplicationUuid/envs" `
                -Headers $coolifyHeaders -ContentType 'application/json' `
                -Body (@{ key = $Key; value = $Value } | ConvertTo-Json -Compress) | Out-Null
            return
        }
        catch {
            $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
            if ($status -ne 409) { throw "Creation aveugle de $Key pour $ApplicationUuid : HTTP $status" }
        }
        foreach ($isPreview in @($false, $true)) {
            Invoke-SafeRest -Method Patch `
                -Uri "$coolifyBase/api/v1/applications/$ApplicationUuid/envs" `
                -Headers $coolifyHeaders `
                -Body @{ key = $Key; value = $Value; is_preview = $isPreview } `
                -Label "Mise a jour aveugle de $Key pour $ApplicationUuid preview=$isPreview" | Out-Null
        }
        return
    }

    foreach ($isPreview in @($false, $true)) {
        # Coolify peut creer automatiquement la variante preview lors du POST.
        # Relire avant chaque ecriture evite donc un second POST en conflit.
        $response = Invoke-SafeRest -Method Get `
            -Uri "$coolifyBase/api/v1/applications/$ApplicationUuid/envs" `
            -Headers $coolifyHeaders -Label "Lecture des variables $ApplicationUuid"
        $current = @()
        foreach ($item in $response) { $current += $item }
        $exists = @($current | Where-Object {
                $_.key -eq $Key -and [bool]$_.is_preview -eq $isPreview
            }).Count -gt 0
        $method = if ($exists) { 'Patch' } else { 'Post' }
        Invoke-SafeRest -Method $method `
            -Uri "$coolifyBase/api/v1/applications/$ApplicationUuid/envs" `
            -Headers $coolifyHeaders `
            -Body @{ key = $Key; value = $Value; is_preview = $isPreview } `
            -Label "Ecriture de $Key pour $ApplicationUuid preview=$isPreview" | Out-Null
    }
}

function Start-Deployment {
    param(
        [Parameter(Mandatory = $true)][string]$ApplicationUuid,
        [Parameter(Mandatory = $true)][string]$Commit
    )

    Invoke-SafeRest -Method Patch `
        -Uri "$coolifyBase/api/v1/applications/$ApplicationUuid" `
        -Headers $coolifyHeaders -Body @{ git_commit_sha = $Commit } `
        -Label "Epinglage de $ApplicationUuid" | Out-Null
    $response = Invoke-SafeRest -Method Post -Uri "$coolifyBase/api/v1/deploy" `
        -Headers $coolifyHeaders -Body @{ uuid = $ApplicationUuid } `
        -Label "Demarrage du deploiement $ApplicationUuid"
    $deploymentUuid = $response.deployments[0].deployment_uuid
    if (-not $deploymentUuid) { throw "Identifiant de deploiement absent pour $ApplicationUuid" }
    return $deploymentUuid
}

function Wait-Deployment {
    param([Parameter(Mandatory = $true)][string]$DeploymentUuid)

    $deadline = (Get-Date).AddMinutes(30)
    while ((Get-Date) -lt $deadline) {
        $deployment = Invoke-SafeRest -Method Get `
            -Uri "$coolifyBase/api/v1/deployments/$DeploymentUuid" `
            -Headers $coolifyHeaders -Label "Lecture du deploiement $DeploymentUuid"
        if ($deployment.status -eq 'finished') { return }
        if ($deployment.status -in @('failed', 'cancelled')) {
            throw "Deploiement $DeploymentUuid termine avec le statut $($deployment.status)"
        }
        Start-Sleep -Seconds 15
    }
    throw "Delai Coolify depasse pour $DeploymentUuid"
}

function Invoke-Diwan {
    param(
        [Parameter(Mandatory = $true)][string]$Token,
        [Parameter(Mandatory = $true)][ValidateSet('Get', 'Post', 'Delete')][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [object]$Body,
        [hashtable]$Form
    )
    return Invoke-SafeRest -Method $Method -Uri "$diwanBase$Path" `
        -Headers @{ Authorization = "Bearer $Token"; Accept = 'application/json' } `
        -Body $Body -Form $Form -Label "Appel Diwan $Method $Path"
}

$configuredMap = $env:QALEM_DIWAN_TENANT_TOKENS | ConvertFrom-Json
$primaryToken = $configuredMap.$QalemOrganizationId
if (-not $primaryToken -or $primaryToken.Length -lt 16) {
    throw "Jeton Qalem absent du mapping pour $QalemOrganizationId"
}
$secondaryToken = New-ServiceToken
$primaryHash = Get-Sha256 -Value $primaryToken
$secondaryHash = Get-Sha256 -Value $secondaryToken
$initialDiwanConfig = "qalem:$QalemOrganizationId`:$primaryHash,qalem:$secondaryOrganizationId`:$secondaryHash"
$finalDiwanConfig = "qalem:$QalemOrganizationId`:$primaryHash"
$qalemTokenMap = $env:QALEM_DIWAN_TENANT_TOKENS
if ($env:DIWAN_CONSUMER_TOKENS -ne $finalDiwanConfig) {
    throw 'La configuration finale du coffre ne correspond pas au jeton Qalem'
}
$deployments = [Collections.Generic.List[string]]::new()
$evidence = [ordered]@{}

try {
    Set-CoolifyEnvironmentValue -ApplicationUuid $DiwanApplicationUuid `
        -Key 'DIWAN_CONSUMER_TOKENS' -Value $initialDiwanConfig
    Set-CoolifyEnvironmentValue -ApplicationUuid $QalemApplicationUuid `
        -Key 'QALEM_DIWAN_TENANT_TOKENS' -Value $qalemTokenMap -Blind

    $diwanDeployment = Start-Deployment -ApplicationUuid $DiwanApplicationUuid -Commit $DiwanCommit
    $qalemDeployment = Start-Deployment -ApplicationUuid $QalemApplicationUuid -Commit $QalemCommit
    $deployments.Add($diwanDeployment)
    $deployments.Add($qalemDeployment)
    Wait-Deployment -DeploymentUuid $diwanDeployment
    Wait-Deployment -DeploymentUuid $qalemDeployment

    $anonymousStatus = Get-HttpStatus -Uri "$diwanBase/sources"
    $primarySources = Invoke-Diwan -Token $primaryToken -Method Get -Path '/sources?page=1&pageSize=20'
    $secondarySources = Invoke-Diwan -Token $secondaryToken -Method Get -Path '/sources?page=1&pageSize=20'
    if ($anonymousStatus -ne 401) { throw "La facade anonyme repond $anonymousStatus au lieu de 401" }
    if ($primarySources.contractVersion -ne '1.0' -or $secondarySources.contractVersion -ne '1.0') {
        throw 'Version de contrat inattendue'
    }

    $idempotencyKey = "recette-s6-003-$([guid]::NewGuid())"
    $fixtureText = @'
Le SIPOC de la formation commence par les fournisseurs, puis les entrees, le processus, les sorties et les clients.
La validation du syllabus exige une preuve documentaire, une decision de l'auteur et une trace horodatee.
Pour un apprentissage adulte, chaque activite relie une situation professionnelle a une action observable.
'@
    $ingestion = Invoke-Diwan -Token $primaryToken -Method Post -Path '/ingestions' -Form @{
        texts = $fixtureText
        corpusName = "Recette Qalem $QalemOrganizationId"
        idempotencyKey = $idempotencyKey
    }
    if ($ingestion.status -ne 'queued' -or $ingestion.submittedSources -ne 1) {
        throw 'Reponse initiale d ingestion inattendue'
    }
    $replay = Invoke-Diwan -Token $primaryToken -Method Post -Path '/ingestions' -Form @{
        texts = $fixtureText
        corpusName = "Recette Qalem $QalemOrganizationId"
        idempotencyKey = $idempotencyKey
    }
    if ($replay.jobId -ne $ingestion.jobId -or $replay.corpusId -ne $ingestion.corpusId) {
        throw 'La cle d idempotence a cree un second import'
    }

    $terminal = $null
    $deadline = (Get-Date).AddMinutes(12)
    while ((Get-Date) -lt $deadline) {
        $terminal = Invoke-Diwan -Token $primaryToken -Method Get `
            -Path "/ingestions/$([uri]::EscapeDataString($ingestion.jobId))"
        if ($terminal.status -in @('ready', 'partially_failed', 'failed')) { break }
        Start-Sleep -Seconds 15
    }
    if (-not $terminal -or $terminal.status -ne 'ready') {
        $state = if ($terminal) { $terminal.status } else { 'timeout' }
        throw "Import non exploitable : $state"
    }
    $sourceId = $terminal.sources[0].sourceId
    if (-not $sourceId -or $terminal.sources[0].chunks -lt 1) {
        throw 'Import pret sans source vectorisee'
    }

    $foreignJobStatus = Get-HttpStatus -Uri "$diwanBase/ingestions/$([uri]::EscapeDataString($ingestion.jobId))" `
        -Headers @{ Authorization = "Bearer $secondaryToken" }
    $foreignCorpusStatus = Get-HttpStatus -Uri "$diwanBase/sources?corpusId=$([uri]::EscapeDataString($ingestion.corpusId))" `
        -Headers @{ Authorization = "Bearer $secondaryToken" }
    if ($foreignJobStatus -ne 404 -or $foreignCorpusStatus -ne 404) {
        throw "Isolement inter-tenant invalide : job=$foreignJobStatus corpus=$foreignCorpusStatus"
    }

    $manifest = Invoke-Diwan -Token $primaryToken -Method Post -Path '/sources/manifest' `
        -Body @{ sourceIds = @($sourceId) }
    if ($manifest.sources.Count -ne 1 -or $manifest.sources[0].sourceId -ne $sourceId) {
        throw 'Manifeste de source incoherent'
    }
    $retrieval = Invoke-Diwan -Token $primaryToken -Method Post -Path '/retrieve' -Body @{
        corpusId = $ingestion.corpusId
        sourceIds = @($sourceId)
        query = 'Quelles sont les etapes du SIPOC ?'
        limit = 12
        minimumScore = 0.0
        searchMode = 'hybrid'
    }
    if ($retrieval.status -ne 'ok' -or $retrieval.evidence.Count -lt 1) {
        throw 'Recherche sans preuve documentaire'
    }
    if (@($retrieval.evidence | Where-Object { $_.sourceId -ne $sourceId }).Count -gt 0) {
        throw 'Recherche ayant depasse la liste blanche de sources'
    }

    # La comparaison exacte de non-divulgation est prouvee par une requete POST.
    try {
        Invoke-Diwan -Token $secondaryToken -Method Post -Path '/sources/manifest' `
            -Body @{ sourceIds = @($sourceId) } | Out-Null
        throw 'Une source du tenant primaire est visible par le tenant secondaire'
    }
    catch {
        if ($_.Exception.Message -notmatch 'HTTP 403') { throw }
    }

    $revoked = Invoke-Diwan -Token $primaryToken -Method Delete `
        -Path "/corpora/$([uri]::EscapeDataString($ingestion.corpusId))"
    if ($revoked.status -ne 'revoked') { throw 'Revocation de corpus non confirmee' }
    $postRevokeStatus = Get-HttpStatus `
        -Uri "$diwanBase/sources?corpusId=$([uri]::EscapeDataString($ingestion.corpusId))" `
        -Headers @{ Authorization = "Bearer $primaryToken" }
    if ($postRevokeStatus -ne 404) { throw "Corpus encore accessible apres revocation : $postRevokeStatus" }

    Set-CoolifyEnvironmentValue -ApplicationUuid $DiwanApplicationUuid `
        -Key 'DIWAN_CONSUMER_TOKENS' -Value $finalDiwanConfig
    $revocationDeployment = Start-Deployment -ApplicationUuid $DiwanApplicationUuid -Commit $DiwanCommit
    $deployments.Add($revocationDeployment)
    Wait-Deployment -DeploymentUuid $revocationDeployment

    $primaryAfterRotation = Get-HttpStatus -Uri "$diwanBase/sources" `
        -Headers @{ Authorization = "Bearer $primaryToken" }
    $secondaryAfterRotation = Get-HttpStatus -Uri "$diwanBase/sources" `
        -Headers @{ Authorization = "Bearer $secondaryToken" }
    if ($primaryAfterRotation -ne 200 -or $secondaryAfterRotation -ne 401) {
        throw "Revocation du tenant de recette invalide : primaire=$primaryAfterRotation secondaire=$secondaryAfterRotation"
    }

    $evidence.anonymousRefused = $anonymousStatus -eq 401
    $evidence.contractVersion = $primarySources.contractVersion
    $evidence.twoTenantIsolation = $foreignJobStatus -eq 404 -and $foreignCorpusStatus -eq 404
    $evidence.idempotency = $replay.jobId -eq $ingestion.jobId
    $evidence.ingestionReady = $terminal.status -eq 'ready'
    $evidence.vectorizedChunks = [int]$terminal.sources[0].chunks
    $evidence.manifestValidated = $manifest.sources.Count -eq 1
    $evidence.retrievalEvidenceCount = @($retrieval.evidence).Count
    $evidence.corpusRevoked = $revoked.status -eq 'revoked'
    $evidence.secondaryCredentialRevoked = $secondaryAfterRotation -eq 401
    $evidence.primaryCredentialActive = $primaryAfterRotation -eq 200
    $evidence.diwanCommit = $DiwanCommit
    $evidence.qalemCommit = $QalemCommit
    $evidence.deployments = @($deployments)
    [pscustomobject]$evidence | ConvertTo-Json -Depth 6 -Compress
}
finally {
    $primaryToken = $null
    $secondaryToken = $null
    $qalemTokenMap = $null
}
