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
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'DPAPI secret vault was not found.' }
    $encoded = ([IO.File]::ReadAllText((Resolve-Path -LiteralPath $Path))).Trim()
    $cipher = [Convert]::FromBase64String($encoded)
    $plain = [Security.Cryptography.ProtectedData]::Unprotect(
        $cipher, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    try {
        $payload = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
        $schema = if ($payload.PSObject.Properties.Name -contains 'schema') { [string]$payload.schema } else { [string]$payload.schema_version }
        if ($schema -ne 'amazon-us-secrets-v1') { throw 'DPAPI secret vault schema is not supported.' }
        $values = if ($payload.PSObject.Properties.Name -contains 'values') { $payload.values } else { $payload }
        return [pscustomobject]@{ Values = $values; Cipher = $cipher; Plain = $plain }
    }
    catch {
        [Array]::Clear($plain, 0, $plain.Length)
        [Array]::Clear($cipher, 0, $cipher.Length)
        throw
    }
}

$material = $null
try {
    $material = Get-Secrets $SecretPath
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.UseShellExecute = $false
    foreach ($argument in $ArgumentList) { $startInfo.ArgumentList.Add([string]$argument) }

    if ($AgentId) {
        if ($SecretNames.Count -ne 4) { throw 'Agent mode cannot inject service secrets.' }
        $master = [string]$material.Values.AMAZON_COLLECTION_API_KEY
        if ([string]::IsNullOrWhiteSpace($master)) { throw 'Collection service key is unavailable.' }
        $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($master))
        try {
            $derived = [Convert]::ToHexString($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("amazon-us-collection-agent-v1:$AgentId"))).ToLowerInvariant()
            $startInfo.Environment['AMAZON_COLLECTION_AGENT_ID'] = $AgentId
            $startInfo.Environment['AMAZON_COLLECTION_AGENT_KEY'] = $derived
        }
        finally { $hmac.Dispose(); $derived = $null; $master = $null }
    }
    else {
        foreach ($name in $SecretNames) {
            $value = [string]$material.Values.$name
            if ([string]::IsNullOrWhiteSpace($value)) { throw "Required secret '$name' is unavailable." }
            $startInfo.Environment[$name] = $value
            $value = $null
        }
    }
    $child = [Diagnostics.Process]::Start($startInfo)
    $child.WaitForExit()
    exit $child.ExitCode
}
catch {
    # Never serialize exception internals: these can contain command line or provider data.
    [Console]::Error.WriteLine('secure launcher failed')
    exit 2
}
finally {
    if ($null -ne $material) {
        if ($material.Plain) { [Array]::Clear($material.Plain, 0, $material.Plain.Length) }
        if ($material.Cipher) { [Array]::Clear($material.Cipher, 0, $material.Cipher.Length) }
    }
}
