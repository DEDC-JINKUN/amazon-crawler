import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).parents[1]


def test_operator_stop_disposition_preserves_observed_worker_exit_and_interruption():
    shell = shutil.which('powershell') or shutil.which('pwsh')
    path = str(ROOT/'crawler.ps1').replace("'", "''")
    command = rf'''
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$tokens,[ref]$errors)
$fn=$ast.FindAll({{param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-RunExitDisposition'}},$true) | Select-Object -First 1
if($null -eq $fn) {{ throw 'missing disposition entry' }}
Invoke-Expression $fn.Extent.Text
Get-RunExitDisposition -1 $true | ConvertTo-Json -Compress
'''
    result = subprocess.run([shell,'-NoProfile','-Command',command],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value == {'status':'interrupted','controller_exit_code':130,'worker_exit_code':-1,'reason':'operator_interrupted'}
