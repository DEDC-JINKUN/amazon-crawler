[CmdletBinding()]
param(
    [Parameter(Position=0)][ValidateSet('start','status','stop','open')][string]$Command='status',
    [ValidateRange(1,65535)][int]$Port=8772,
    [ValidateRange(1,65535)][int]$AgentPort=8765,
    [ValidateRange(1,65535)][int]$InternalConsolePort=8773,
    [ValidateRange(1,65535)][int]$DashboardPort=8770,
    [ValidatePattern('^[A-Za-z0-9_.-]{1,120}$')][string]$BusinessTenantId='amazon_us_main',
    [ValidateRange(60,604800)][int]$CampaignDeadlineSeconds=86400,
    [string]$DataRoot='data\operator',
    [string]$ConfigPath='config\amazon_us.windows.toml',
    [string]$VaultPath='data\secure\amazon_us.secrets.dpapi',
    [switch]$FakeRunner,
    [switch]$PrintUrl,
    [ValidateRange(10,5000)][int]$StopWaitMilliseconds=5000,
    [ValidateRange(1,40)][int]$StartHealthAttempts=40
)

Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
$python=Join-Path $root '.venv\Scripts\python.exe'
$service=Join-Path $root 'scripts\collection_operator.py'
$serviceStarter=Join-Path $root 'scripts\operator_service_start.py'

function Resolve-RootPath([string]$Value,[bool]$MustExist=$true){
    $candidate=if([IO.Path]::IsPathRooted($Value)){$Value}else{Join-Path $root $Value}
    if($MustExist){return (Resolve-Path -LiteralPath $candidate).Path}
    return [IO.Path]::GetFullPath($candidate)
}
function Read-Lock([string]$Path){
    if(-not(Test-Path -LiteralPath $Path -PathType Leaf)){return $null}
    try{return Get-Content -LiteralPath $Path -Raw|ConvertFrom-Json}catch{return $null}
}
function Get-VerifiedProcess([object]$Lock){
    if($null-eq$Lock-or-not$Lock.pid-or-not$Lock.start_time){return $null}
    try{$process=Get-Process -Id ([int]$Lock.pid) -ErrorAction Stop}catch{return $null}
    try{
        if($Lock.start_time-is[DateTime]){$expected=([DateTime]$Lock.start_time).ToUniversalTime()}
        elseif($Lock.start_time-is[DateTimeOffset]){$expected=([DateTimeOffset]$Lock.start_time).UtcDateTime}
        else{$expected=([DateTimeOffset]::Parse([string]$Lock.start_time)).UtcDateTime}
        $actual=$process.StartTime.ToUniversalTime()
    }catch{return $null}
    if($expected.Ticks-ne$actual.Ticks){return $null}
    return $process
}
function Write-JsonAtomic([object]$Value,[string]$Path){
    $temporary="${Path}.${PID}.tmp"
    $Value|ConvertTo-Json -Depth 6|Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}
function Get-ControlMutexName {
    $sha=[Security.Cryptography.SHA256]::Create()
    try{
        $normalizedRoot=[IO.Path]::GetFullPath($root).TrimEnd([IO.Path]::DirectorySeparatorChar,[IO.Path]::AltDirectorySeparatorChar).ToLowerInvariant()
        $normalizedData=[IO.Path]::GetFullPath($data).TrimEnd([IO.Path]::DirectorySeparatorChar,[IO.Path]::AltDirectorySeparatorChar).ToLowerInvariant()
        $bytes=[Text.Encoding]::UTF8.GetBytes("${normalizedRoot}|${normalizedData}|${Port}")
        $hash=([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').Substring(0,24)
        return "Local\AmazonCrawlerOperatorControl_${hash}"
    }
    finally{$sha.Dispose()}
}
function Acquire-ControlMutex {
    $mutex=[Threading.Mutex]::new($false,(Get-ControlMutexName))
    $acquired=$false
    try{$acquired=$mutex.WaitOne(5000)}
    catch [Threading.AbandonedMutexException]{$acquired=$true}
    if(-not$acquired){$mutex.Dispose();throw 'Operator is already starting, stopping, or running.'}
    return $mutex
}
function Release-ControlMutex([Threading.Mutex]$Mutex){
    if($null-eq$Mutex){return}
    try{$Mutex.ReleaseMutex()}catch{}
    $Mutex.Dispose()
}
function Test-LocalPortListening([int]$LocalPort){
    return @([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
        Where-Object { $_.Port -eq $LocalPort }).Count -gt 0
}
function Test-SameLock([object]$Left,[object]$Right){
    if($null-eq$Left-and$null-eq$Right){return $true}
    if($null-eq$Left-or$null-eq$Right){return $false}
    return [int]$Left.pid-eq[int]$Right.pid-and[string]$Left.start_time-eq[string]$Right.start_time
}

$data=Resolve-RootPath $DataRoot $false
$control=Join-Path $data 'control'
$lockPath=Join-Path $control 'operator.lock.json'
$url="http://127.0.0.1:$Port"

if($Command-eq'open'){
    if($PrintUrl){$url;exit 0}
    $startInfo=[Diagnostics.ProcessStartInfo]::new($url)
    $startInfo.UseShellExecute=$true
    [Diagnostics.Process]::Start($startInfo)|Out-Null
    exit 0
}

$lock=Read-Lock $lockPath
$process=Get-VerifiedProcess $lock
if($Command-eq'status'){
    $healthy=$false
    if($null-ne$process){try{$healthy=(Invoke-RestMethod -Uri "$url/healthz" -TimeoutSec 2).ok-eq$true}catch{}}
    [ordered]@{running=$null-ne$process;healthy=$healthy;port=$Port;pid=if($process){$process.Id}else{$null}}|ConvertTo-Json
    exit $(if($healthy){0}else{1})
}
if($Command-eq'stop'){
    $controlMutex=$null
    $stopExitCode=0
    $stopError=$null
    try{
        $controlMutex=Acquire-ControlMutex
        $lock=Read-Lock $lockPath
        $process=Get-VerifiedProcess $lock
        if($null-ne$process){
            Stop-Process -Id $process.Id -ErrorAction Stop
            $exited=$process.WaitForExit($StopWaitMilliseconds)
            if(-not$exited){
                $verified=Get-VerifiedProcess $lock
                if($null-ne$verified){
                    Stop-Process -Id $verified.Id -Force -ErrorAction Stop
                    $verified.WaitForExit($StopWaitMilliseconds)|Out-Null
                }
            }
        }
        $remaining=Get-VerifiedProcess $lock
        $portBusy=$false
        for($attempt=0;$attempt-lt20;$attempt++){
            $remaining=Get-VerifiedProcess $lock
            $portBusy=Test-LocalPortListening $Port
            if($null-eq$remaining-and-not$portBusy){break}
            Start-Sleep -Milliseconds 100
        }
        if($null-ne$remaining-or$portBusy){
            $stopExitCode=2
            $stopError='Operator stop could not verify process and port release; lock retained.'
        }
        else{
            $currentLock=Read-Lock $lockPath
            if(-not(Test-SameLock $lock $currentLock)){
                $stopExitCode=2
                $stopError='Operator lock changed during stop; current lock retained.'
            }
            elseif($null-ne$currentLock){Remove-Item -LiteralPath $lockPath -Force}
        }
    }
    finally{Release-ControlMutex $controlMutex}
    if($stopExitCode-ne0){
        [Console]::Error.WriteLine($stopError)
        exit $stopExitCode
    }
    'Operator stopped; active crawler batches are unchanged.'
    exit 0
}

$startMutex=$null
$ownedProcess=$null
try{
    $startMutex=Acquire-ControlMutex
    $lock=Read-Lock $lockPath
    $process=Get-VerifiedProcess $lock
    if($null-ne$process){throw 'Operator is already running.'}
    if(Test-LocalPortListening $Port){throw 'Operator port is already occupied.'}
    if(-not(Test-Path -LiteralPath $python -PathType Leaf)){throw 'Operator Python runtime is unavailable.'}
    if(-not(Test-Path -LiteralPath $serviceStarter -PathType Leaf)){throw 'Operator service starter is unavailable.'}
    $config=Resolve-RootPath $ConfigPath
    $vault=Resolve-RootPath $VaultPath
    [IO.Directory]::CreateDirectory($control)|Out-Null
    $stdout=Join-Path $control 'operator.stdout.log'
    $stderr=Join-Path $control 'operator.stderr.log'
    $startArguments=@($serviceStarter,'--python',$python,'--service',$service,'--working-directory',$root,
        '--data-root',$data,'--config',$config,'--vault',$vault,'--stdout',$stdout,'--stderr',$stderr,
        '--port',[string]$Port,'--agent-port',[string]$AgentPort,'--internal-console-port',[string]$InternalConsolePort,
        '--dashboard-port',[string]$DashboardPort,
        '--business-tenant-id',$BusinessTenantId,
        '--campaign-deadline-seconds',[string]$CampaignDeadlineSeconds)
    if($FakeRunner){$startArguments+='--fake-runner'}
    $startResult=& $python @startArguments
    if($LASTEXITCODE-ne0){throw 'Operator service process could not be started.'}
    try{$started=$startResult|ConvertFrom-Json;$ownedProcess=Get-Process -Id ([int]$started.pid) -ErrorAction Stop}
    catch{throw 'Operator service process identity is unavailable.'}
    Write-JsonAtomic ([ordered]@{schema='amazon-operator-lock-v1';pid=$ownedProcess.Id;start_time=$ownedProcess.StartTime.ToUniversalTime().ToString('o');port=$Port}) $lockPath
    $ready=$false
    for($attempt=0;$attempt-lt$StartHealthAttempts;$attempt++){
        try{if((Invoke-RestMethod -Uri "$url/healthz" -TimeoutSec 1).ok){$ready=$true;break}}catch{}
        Start-Sleep -Milliseconds 250
    }
    if(-not$ready){throw 'Operator did not become healthy.'}
    "Operator ready: $url"
}
catch{
    if($null-ne$ownedProcess){
        $ownedLock=Read-Lock $lockPath
        $verified=Get-VerifiedProcess $ownedLock
        if($null-ne$verified-and$verified.Id-eq$ownedProcess.Id){Stop-Process -Id $verified.Id -Force -ErrorAction SilentlyContinue}
        elseif(-not$ownedProcess.HasExited){Stop-Process -Id $ownedProcess.Id -Force -ErrorAction SilentlyContinue}
    }
    if(Test-Path -LiteralPath $lockPath){
        $failedLock=Read-Lock $lockPath
        if($null-eq$failedLock-or($null-ne$ownedProcess-and[int]$failedLock.pid-eq$ownedProcess.Id)){
            Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}
finally{Release-ControlMutex $startMutex}
