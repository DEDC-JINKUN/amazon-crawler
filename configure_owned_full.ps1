[CmdletBinding()]
param(
    [ValidateSet('Configure','Verify','Rotate')][string]$Mode = 'Configure',
    [string]$SecretPath = (Join-Path $PSScriptRoot 'data\secure\amazon_us.secrets.dpapi'),
    [switch]$RotateApiKey
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[void][System.Reflection.Assembly]::LoadWithPartialName('System.Security')

function Get-CurrentUserSid {
    return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}

function Read-SecretText([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $secure.Dispose()
    }
}

function Get-CipherGeneration([byte[]]$Cipher) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hex = [BitConverter]::ToString($sha.ComputeHash($Cipher)).Replace('-', '').ToLowerInvariant()
        return 'legacy-' + $hex.Substring(0, 32)
    }
    finally { $sha.Dispose() }
}

function Read-Vault([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'vault_not_found' }
    $encoded = ([IO.File]::ReadAllText((Resolve-Path -LiteralPath $Path))).Trim()
    $generation = $null
    if ($encoded.StartsWith('{')) {
        $envelope = $encoded | ConvertFrom-Json
        if ([string]$envelope.schema -ne 'amazon-us-dpapi-envelope-v1' -or
            [string]$envelope.scope -ne 'CurrentUser') { throw 'vault_envelope_invalid' }
        if ([string]$envelope.owner_sid -ne (Get-CurrentUserSid)) { throw 'vault_owner_mismatch' }
        if ($envelope.PSObject.Properties.Name -contains 'credential_generation') {
            $generation = [string]$envelope.credential_generation
        }
        $encoded = [string]$envelope.ciphertext
    }
    $cipher = [Convert]::FromBase64String($encoded)
    if ([string]::IsNullOrWhiteSpace($generation)) { $generation = Get-CipherGeneration $cipher }
    if ($generation -notmatch '^[A-Za-z0-9_.:-]{8,100}$') { throw 'vault_generation_invalid' }
    $plain = $null
    try {
        $plain = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $cipher, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        $payload = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
        $schema = if ($payload.PSObject.Properties.Name -contains 'schema') {
            [string]$payload.schema
        } else {
            [string]$payload.schema_version
        }
        if ($schema -ne 'amazon-us-secrets-v1') { throw 'vault_payload_invalid' }
        $values = if ($payload.PSObject.Properties.Name -contains 'values') { $payload.values } else { $payload }
        foreach ($name in @('AMAZON_PROXY_USER','AMAZON_PROXY_PASS','AMAZON_US_POSTGRES_DSN','AMAZON_COLLECTION_API_KEY')) {
            if ([string]::IsNullOrWhiteSpace([string]$values.$name)) { throw 'vault_payload_incomplete' }
        }
        return [pscustomobject]@{ Values = $values; Generation = $generation }
    }
    finally {
        if ($null -ne $plain) { [Array]::Clear($plain, 0, $plain.Length) }
        if ($null -ne $cipher) { [Array]::Clear($cipher, 0, $cipher.Length) }
    }
}

function Set-VaultAcl([string]$Path, [string]$OwnerSid) {
    $owner = [Security.Principal.SecurityIdentifier]::new($OwnerSid)
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($owner, $system)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow)
        [void]$acl.AddAccessRule($rule)
    }
    [IO.File]::SetAccessControl($Path, $acl)
}

function New-RandomApiKey {
    $random = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($random)
        return [Convert]::ToBase64String($random)
    }
    finally {
        $generator.Dispose()
        [Array]::Clear($random, 0, $random.Length)
    }
}

$plain = $null
$cipher = $null
$temporary = $null
$existing = $null
$proxyLogin = $null
$proxyPassword = $null
$postgresPassword = $null
$postgresDsn = $null
$apiKey = $null
$payload = $null
$verification = $null
$credentialGeneration = $null
$script:CredentialStage = 'start'
try {
    if ($Mode -eq 'Verify') {
        $existing = Read-Vault $SecretPath
        Write-Host 'Local crawler credential vault verified for the current Windows user.'
        exit 0
    }

    if ((Test-Path -LiteralPath $SecretPath -PathType Leaf) -and $Mode -eq 'Configure') {
        try {
            $existing = Read-Vault $SecretPath
            Write-Host 'Local crawler credentials are already configured for the current Windows user.'
            exit 0
        }
        catch {
            Write-Host 'The existing vault is not usable by this Windows user; it will be replaced after credential entry.'
        }
    }
    elseif ((Test-Path -LiteralPath $SecretPath -PathType Leaf) -and $Mode -eq 'Rotate') {
        try { $existing = Read-Vault $SecretPath } catch { $existing = $null }
    }

    $script:CredentialStage = 'read-environment'
    $proxyLogin = [Environment]::GetEnvironmentVariable('AMAZON_PROXY_USER', 'Process')
    $proxyPassword = [Environment]::GetEnvironmentVariable('AMAZON_PROXY_PASS', 'Process')
    if ([string]::IsNullOrWhiteSpace($proxyLogin)) { $proxyLogin = Read-SecretText 'DataImpulse Login' }
    if ([string]::IsNullOrWhiteSpace($proxyPassword)) { $proxyPassword = Read-SecretText 'DataImpulse Password' }

    $postgresDsn = [Environment]::GetEnvironmentVariable('AMAZON_US_POSTGRES_DSN', 'Process')
    if ([string]::IsNullOrWhiteSpace($postgresDsn)) {
        $postgresPassword = Read-SecretText 'PostgreSQL postgres Password'
        if ([string]::IsNullOrWhiteSpace($postgresPassword)) { throw 'postgres_credential_missing' }
        $postgresDsn = "host=127.0.0.1 port=5432 dbname=postgres user=postgres password=$postgresPassword"
    }
    if ([string]::IsNullOrWhiteSpace($proxyLogin) -or [string]::IsNullOrWhiteSpace($proxyPassword)) {
        throw 'proxy_credential_missing'
    }
    if ($proxyLogin -match '__cr\.' -and $proxyLogin -notlike '*__cr.us') {
        throw 'proxy_country_not_us'
    }
    $proxyUser = if ($proxyLogin -like '*__cr.us') { $proxyLogin } else { "${proxyLogin}__cr.us" }

    if (-not $RotateApiKey -and $null -ne $existing) {
        $apiKey = [string]$existing.Values.AMAZON_COLLECTION_API_KEY
    }
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        $apiKey = [Environment]::GetEnvironmentVariable('AMAZON_COLLECTION_API_KEY', 'Process')
    }
    if ([string]::IsNullOrWhiteSpace($apiKey)) { $apiKey = New-RandomApiKey }

    $script:CredentialStage = 'build-payload'
    $payload = [ordered]@{
        schema_version = 'amazon-us-secrets-v1'
        AMAZON_PROXY_USER = $proxyUser
        AMAZON_PROXY_PASS = $proxyPassword
        AMAZON_US_POSTGRES_DSN = $postgresDsn
        AMAZON_COLLECTION_API_KEY = $apiKey
    } | ConvertTo-Json -Compress
    $plain = [Text.Encoding]::UTF8.GetBytes($payload)
    $script:CredentialStage = 'protect'
    $cipher = [System.Security.Cryptography.ProtectedData]::Protect(
        $plain, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)

    $ownerSid = Get-CurrentUserSid
    $credentialGeneration = [guid]::NewGuid().ToString('N')
    $envelope = [ordered]@{
        schema = 'amazon-us-dpapi-envelope-v1'
        scope = 'CurrentUser'
        owner_sid = $ownerSid
        created_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
        credential_generation = $credentialGeneration
        ciphertext = [Convert]::ToBase64String($cipher)
    } | ConvertTo-Json -Compress

    $parent = Split-Path -Parent $SecretPath
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temporary = Join-Path $parent ('.amazon_us.secrets.{0}.tmp' -f [guid]::NewGuid().ToString('N'))
    $script:CredentialStage = 'write-temporary'
    [IO.File]::WriteAllText($temporary, $envelope, [Text.Encoding]::ASCII)
    $script:CredentialStage = 'acl-temporary'
    Set-VaultAcl $temporary $ownerSid
    $script:CredentialStage = 'verify-temporary'
    $verification = Read-Vault $temporary
    $script:CredentialStage = 'replace-vault'
    Move-Item -LiteralPath $temporary -Destination $SecretPath -Force
    $temporary = $null
    $script:CredentialStage = 'acl-vault'
    Set-VaultAcl $SecretPath $ownerSid
    Write-Host 'Local crawler credentials configured and verified for the current Windows user.'
    exit 0
}
catch {
    $reason = [string]$_.Exception.Message
    if ($reason -notin @('vault_not_found','vault_envelope_invalid','vault_owner_mismatch','vault_payload_invalid',
            'vault_payload_incomplete','vault_generation_invalid','postgres_credential_missing','proxy_credential_missing','proxy_country_not_us')) {
        $errorType = $_.Exception.GetType().Name
        $errorCode = ('0x{0:X8}' -f ($_.Exception.HResult -band 0xffffffffL))
        $reason = 'credential_operation_failed:{0}:{1}:{2}' -f $script:CredentialStage, $errorType, $errorCode
    }
    [Console]::Error.WriteLine("Local crawler credential operation failed: $reason")
    exit 2
}
finally {
    if ($temporary -and (Test-Path -LiteralPath $temporary)) {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $plain) { [Array]::Clear($plain, 0, $plain.Length) }
    if ($null -ne $cipher) { [Array]::Clear($cipher, 0, $cipher.Length) }
    $proxyLogin = $null
    $proxyUser = $null
    $proxyPassword = $null
    $postgresPassword = $null
    $postgresDsn = $null
    $apiKey = $null
    $payload = $null
    $existing = $null
    $verification = $null
    $credentialGeneration = $null
}
