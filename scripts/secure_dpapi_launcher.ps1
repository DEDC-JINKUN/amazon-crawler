[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [string[]]$ArgumentList = @(),
    [string]$SecretPath = (Join-Path $PSScriptRoot '..\data\secure\amazon_us.secrets.dpapi'),
    [ValidateSet('AMAZON_PROXY_USER','AMAZON_PROXY_PASS','AMAZON_US_POSTGRES_DSN','AMAZON_COLLECTION_API_KEY')]
    [string[]]$SecretNames = @('AMAZON_PROXY_USER','AMAZON_PROXY_PASS','AMAZON_US_POSTGRES_DSN','AMAZON_COLLECTION_API_KEY'),
    [ValidateSet('read-agent','refresh-agent')][string]$AgentId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-QuotedWindowsArgument([string]$Value) {
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + [regex]::Replace($Value, '(\\*)"', '$1$1\"').Replace('\\"', '\\\"') + '"'
}

function Get-CipherGeneration([byte[]]$Cipher) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hex = [BitConverter]::ToString($sha.ComputeHash($Cipher)).Replace('-', '').ToLowerInvariant()
        return 'legacy-' + $hex.Substring(0, 32)
    }
    finally { $sha.Dispose() }
}

function Get-Secrets([string]$Path) {
    $script:LauncherStage = 'get-secrets-path'
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'DPAPI secret vault was not found.' }
    $script:LauncherStage = 'get-secrets-read'
    $encoded = ([IO.File]::ReadAllText((Resolve-Path -LiteralPath $Path))).Trim()
    $generation = $null
    if ($encoded.StartsWith('{')) {
        $script:LauncherStage = 'vault-envelope'
        $envelope = $encoded | ConvertFrom-Json
        if ([string]$envelope.schema -ne 'amazon-us-dpapi-envelope-v1' -or
            [string]$envelope.scope -ne 'CurrentUser') { throw 'Unsupported DPAPI vault envelope.' }
        $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        if ([string]$envelope.owner_sid -ne $currentSid) {
            $script:LauncherStage = 'vault-owner-mismatch'
            throw 'DPAPI vault owner does not match the current Windows account.'
        }
        if ($envelope.PSObject.Properties.Name -contains 'credential_generation') {
            $generation = [string]$envelope.credential_generation
        }
        $encoded = [string]$envelope.ciphertext
    }
    $script:LauncherStage = 'get-secrets-ciphertext'
    $cipher = [Convert]::FromBase64String($encoded)
    if ([string]::IsNullOrWhiteSpace($generation)) { $generation = Get-CipherGeneration $cipher }
    if ($generation -notmatch '^[A-Za-z0-9_.:-]{8,100}$') { throw 'DPAPI vault credential generation is invalid.' }
    $script:LauncherStage = 'get-secrets-unprotect'
    [void][System.Reflection.Assembly]::LoadWithPartialName('System.Security')
    $plain = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $cipher, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    try {
        $script:LauncherStage = 'get-secrets-json'
        $payload = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
        $schema = if ($payload.PSObject.Properties.Name -contains 'schema') { [string]$payload.schema } else { [string]$payload.schema_version }
        if ($schema -ne 'amazon-us-secrets-v1') { throw 'DPAPI secret vault schema is not supported.' }
        $values = if ($payload.PSObject.Properties.Name -contains 'values') { $payload.values } else { $payload }
        $script:LauncherStage = 'get-secrets-return'
        return [pscustomobject]@{ Values = $values; Generation = $generation; Cipher = $cipher; Plain = $plain }
    }
    catch {
        [Array]::Clear($plain, 0, $plain.Length)
        [Array]::Clear($cipher, 0, $cipher.Length)
        throw
    }
}

$material = $null
$originalEnvironment = @{}
$injectedEnvironmentNames = [Collections.Generic.List[string]]::new()
$script:LauncherStage = 'get-secrets'
try {
    $material = Get-Secrets $SecretPath
    $script:LauncherStage = 'environment'
    if ($AgentId) {
        if ($SecretNames.Count -ne 4) { throw 'Agent mode cannot inject service secrets.' }
        $master = [string]$material.Values.AMAZON_COLLECTION_API_KEY
        if ([string]::IsNullOrWhiteSpace($master)) { throw 'Collection service key is unavailable.' }
        $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($master))
        try {
            $derived = [BitConverter]::ToString(
                $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("amazon-us-collection-agent-v1:$AgentId"))
            ).Replace('-', '').ToLowerInvariant()
            foreach ($pair in @(@('AMAZON_COLLECTION_AGENT_ID',$AgentId),@('AMAZON_COLLECTION_AGENT_KEY',$derived))) {
                $name = [string]$pair[0]
                $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
                [Environment]::SetEnvironmentVariable($name, [string]$pair[1], 'Process')
                $injectedEnvironmentNames.Add($name)
            }
        }
        finally { $hmac.Dispose(); $derived = $null; $master = $null }
    }
    else {
        $script:LauncherStage = 'environment-generation'
        $generationName = 'AMAZON_PROXY_CREDENTIAL_GENERATION'
        $originalEnvironment[$generationName] = [Environment]::GetEnvironmentVariable($generationName, 'Process')
        [Environment]::SetEnvironmentVariable($generationName, [string]$material.Generation, 'Process')
        $injectedEnvironmentNames.Add($generationName)
        foreach ($name in $SecretNames) {
            $script:LauncherStage = "environment-read-$name"
            $value = [string]$material.Values.$name
            if ([string]::IsNullOrWhiteSpace($value)) { throw "Required secret '$name' is unavailable." }
            $script:LauncherStage = "environment-set-$name"
            $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
            $injectedEnvironmentNames.Add($name)
            $value = $null
        }
    }
    $script:LauncherStage = 'process-start'
    & $FilePath @ArgumentList
    exit $LASTEXITCODE
}
catch {
    # Never serialize exception internals: these can contain command line or provider data.
    $errorType = $_.Exception.GetType().Name
    $errorCode = ('0x{0:X8}' -f ($_.Exception.HResult -band 0xffffffffL))
    if ($script:LauncherStage -in @('vault-owner-mismatch','get-secrets-unprotect')) {
        [Console]::Error.WriteLine("secure launcher failed at $script:LauncherStage ($errorType/$errorCode); run .\run_owned_full_secure.ps1 configure as the intended Windows account")
    }
    else {
        [Console]::Error.WriteLine("secure launcher failed at $script:LauncherStage ($errorType/$errorCode)")
    }
    exit 2
}
finally {
    foreach ($name in $injectedEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process')
    }
    if ($null -ne $material) {
        if ($material.Plain) { [Array]::Clear($material.Plain, 0, $material.Plain.Length) }
        if ($material.Cipher) { [Array]::Clear($material.Cipher, 0, $material.Cipher.Length) }
    }
}
