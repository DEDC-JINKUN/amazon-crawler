[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ReleaseRoot,
    [Parameter(Mandatory=$true)][string]$OperatorDataRoot,
    [Parameter(Mandatory=$true)][string]$BatchDir,
    [Parameter(Mandatory=$true)][string]$ManifestPath,
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [Parameter(Mandatory=$true)][string]$VaultPath,
    [Parameter(Mandatory=$true)][string]$TenantId,
    [Parameter(Mandatory=$true)][ValidateSet('own','competitor','candidate')][string]$SubjectType,
    [Parameter(Mandatory=$true)][string]$BatchId,
    [ValidateSet(1,2)][int]$WorkerCount=1,
    [ValidateRange(1,5000)][int]$Limit,
    [ValidateRange(1,65535)][int]$InternalConsolePort=8773,
    [ValidateRange(1,5400)][int]$RecoveryMaxSeconds=5400,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [switch]$SecretsLoaded
)

Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'

function Resolve-RequiredFile([string]$Value){
    if(-not(Test-Path -LiteralPath $Value -PathType Leaf)){throw 'required_file_unavailable'}
    return (Resolve-Path -LiteralPath $Value).Path
}
function Resolve-RequiredDirectory([string]$Value){
    if(-not(Test-Path -LiteralPath $Value -PathType Container)){throw 'required_directory_unavailable'}
    return (Resolve-Path -LiteralPath $Value).Path
}
function Assert-DirectChild([string]$Parent,[string]$Child,[string]$Name){
    if([IO.Path]::GetDirectoryName($Child)-ne$Parent){throw "unsafe_${Name}_path"}
}
$root=Resolve-RequiredDirectory $ReleaseRoot
$operatorData=Resolve-RequiredDirectory $OperatorDataRoot
$batch=Resolve-RequiredDirectory $BatchDir
if((Split-Path $batch -Leaf)-ne$BatchId -or $BatchId-notmatch'^batch-[0-9a-f]{24}$'){throw 'invalid_batch_identity'}
if([IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($batch))-ne$operatorData){throw 'batch escaped operator data root'}
$manifest=Resolve-RequiredFile $ManifestPath
$config=Resolve-RequiredFile $ConfigPath
$python=Resolve-RequiredFile $PythonPath
Assert-DirectChild $batch $manifest 'manifest'
Assert-DirectChild $batch $config 'config'
$launcher=Resolve-RequiredFile (Join-Path $root 'scripts\secure_dpapi_launcher.ps1')
$self=Resolve-RequiredFile $PSCommandPath
$controllerHost=Resolve-RequiredFile (Join-Path $root 'scripts\operator_controller_host.py')
$vault=Resolve-RequiredFile $VaultPath

if(-not$SecretsLoaded){
    $shell=(Get-Process -Id $PID).Path
    $inner=@('-NoProfile','-ExecutionPolicy','Bypass','-File',$self,
        '-ReleaseRoot',$root,'-OperatorDataRoot',$operatorData,'-BatchDir',$batch,'-ManifestPath',$manifest,'-ConfigPath',$config,
        '-VaultPath',$vault,'-TenantId',$TenantId,'-SubjectType',$SubjectType,'-BatchId',$BatchId,'-WorkerCount',[string]$WorkerCount,
        '-Limit',[string]$Limit,'-InternalConsolePort',[string]$InternalConsolePort,
        '-RecoveryMaxSeconds',[string]$RecoveryMaxSeconds,'-PythonPath',$python,'-SecretsLoaded')
    & $launcher -SecretPath $vault -SecretNames @('AMAZON_PROXY_USER','AMAZON_PROXY_PASS','AMAZON_US_POSTGRES_DSN') `
        -FilePath $shell -ArgumentList $inner
    exit $LASTEXITCODE
}

if([string]::IsNullOrWhiteSpace($env:AMAZON_US_POSTGRES_DSN)){throw 'secure_environment_unavailable'}
& $python -B $controllerHost --release-root $root --operator-data-root $operatorData --batch-dir $batch --manifest $manifest --config $config `
    --tenant-id $TenantId --subject-type $SubjectType --batch-id $BatchId --worker-count $WorkerCount --limit $Limit `
    --console-port $InternalConsolePort --recovery-seconds $RecoveryMaxSeconds `
    --powershell ((Get-Process -Id $PID).Path)
exit $LASTEXITCODE
