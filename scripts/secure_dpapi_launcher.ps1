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

function Get-Secrets([string]$Path) {
    $script:LauncherStage = 'get-secrets-path'
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'DPAPI secret vault was not found.' }
    $script:LauncherStage = 'get-secrets-read'
    $encoded = ([IO.File]::ReadAllText((Resolve-Path -LiteralPath $Path))).Trim()
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
        $encoded = [string]$envelope.ciphertext
    }
    $script:LauncherStage = 'get-secrets-ciphertext'
    $cipher = [Convert]::FromBase64String($encoded)
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
        return [pscustomobject]@{ Values = $values; Cipher = $cipher; Plain = $plain }
    }
    catch {
        [Array]::Clear($plain, 0, $plain.Length)
        [Array]::Clear($cipher, 0, $cipher.Length)
        throw
    }
}

$material = $null
$script:LauncherStage = 'get-secrets'
try {
    $material = Get-Secrets $SecretPath
    $script:LauncherStage = 'process-start-info'
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.UseShellExecute = $false
    if ($startInfo.PSObject.Properties.Name -contains 'ArgumentList') {
        foreach ($argument in $ArgumentList) { $startInfo.ArgumentList.Add([string]$argument) }
    }
    else {
        $startInfo.Arguments = (($ArgumentList | ForEach-Object {
            ConvertTo-QuotedWindowsArgument ([string]$_)
        }) -join ' ')
    }

    $script:LauncherStage = 'environment'
    if ($AgentId) {
        if ($SecretNames.Count -ne 4) { throw 'Agent mode cannot inject service secrets.' }
        $master = [string]$material.Values.AMAZON_COLLECTION_API_KEY
        if ([string]::IsNullOrWhiteSpace($master)) { throw 'Collection service key is unavailable.' }
        $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($master))
        try {
            $derived = [Convert]::ToHexString($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("amazon-us-collection-agent-v1:$AgentId"))).ToLowerInvariant()
            $startInfo.EnvironmentVariables['AMAZON_COLLECTION_AGENT_ID'] = $AgentId
            $startInfo.EnvironmentVariables['AMAZON_COLLECTION_AGENT_KEY'] = $derived
        }
        finally { $hmac.Dispose(); $derived = $null; $master = $null }
    }
    else {
        foreach ($name in $SecretNames) {
            $script:LauncherStage = "environment-read-$name"
            $value = [string]$material.Values.$name
            if ([string]::IsNullOrWhiteSpace($value)) { throw "Required secret '$name' is unavailable." }
            $script:LauncherStage = "environment-set-$name"
            $startInfo.EnvironmentVariables[$name] = $value
            $value = $null
        }
    }
    $script:LauncherStage = 'process-start'
    $child = [Diagnostics.Process]::Start($startInfo)
    $script:LauncherStage = 'process-wait'
    $child.WaitForExit()
    exit $child.ExitCode
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
    if ($null -ne $material) {
        if ($material.Plain) { [Array]::Clear($material.Plain, 0, $material.Plain.Length) }
        if ($material.Cipher) { [Array]::Clear($material.Cipher, 0, $material.Cipher.Length) }
    }
}
