<#
.SYNOPSIS
Configures and publishes the existing Azure Function App (timer automation) without provisioning new infrastructure.

.DESCRIPTION
Reads secrets from .env, applies Function App settings (AUTOMATION_REPOS, schedule, etc.), and publishes code.
Assumes the Function App and its resource group already exist.
Supports both API key and Managed Identity authentication for Foundry APIs.

.PARAMETER ResourceGroup
Azure resource group containing the Function App (default: jedimaster-rg)

.PARAMETER FunctionAppName
Name of the existing Function App (default: JediMaster)

.PARAMETER CreateIssues
If supplied, enables issue creation (CREATE_ISSUES=1, CREATE_ISSUES_COUNT=3)

.PARAMETER ScheduleCron
Override timer schedule (default: 0 0 */6 * * *)

.EXAMPLE
pwsh ./deploy_existing.ps1

.EXAMPLE
pwsh ./deploy_existing.ps1 -CreateIssues -ScheduleCron "0 0 * * * *"   # hourly

.NOTES
Requires: Azure CLI (az), Functions Core Tools (func), .env with GITHUB_TOKEN, AZURE_AI_FOUNDRY_ENDPOINT, and either AZURE_AI_FOUNDRY_API_KEY (for API key auth) or USE_MANAGED_IDENTITY=1 (for Managed Identity auth).
If using Managed Identity, assign the identity to the Function App and grant it access to Foundry APIs. AZURE_AI_FOUNDRY_API_KEY is not required in this case.
#>
param(
  [string]$ResourceGroup = "jedimaster-rg",
  [string]$FunctionAppName = "JediMaster",
  [switch]$CreateIssues,
  [string]$ScheduleCron = "0 0 */6 * * *"
)

$ErrorActionPreference = "Stop"
Write-Host "=== Updating existing Function App ($FunctionAppName) in RG $ResourceGroup ===" -ForegroundColor Cyan

# Load .env
$envPath = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path $envPath)) { throw ".env not found at $envPath" }
$dotenv = Get-Content $envPath | Where-Object { $_ -and ($_ -notmatch '^#') -and ($_ -match '=') }
$envMap = @{}
foreach($line in $dotenv){ $k,$v = $line -split '=',2; $envMap[$k.Trim()] = $v.Trim() }
function Require($k){ if(-not $envMap.ContainsKey($k)){ throw "Missing $k in .env" }; return $envMap[$k] }


$github = Require "GITHUB_TOKEN"
$azureEndpoint = Require "AZURE_AI_FOUNDRY_ENDPOINT"
$useManagedIdentity = $false
if ($envMap.ContainsKey("USE_MANAGED_IDENTITY") -and $envMap["USE_MANAGED_IDENTITY"] -eq '1') {
  $useManagedIdentity = $true
}
$azureApiKey = $null
if (-not $useManagedIdentity) {
  $azureApiKey = Require "AZURE_AI_FOUNDRY_API_KEY"
}

function Get-Opt($k,$default){ if($envMap.ContainsKey($k) -and $envMap[$k]){ return $envMap[$k] } return $default }

$repos        = Get-Opt 'AUTOMATION_REPOS' 'lucabol/Hello-World'
$cronFromEnv  = Get-Opt 'SCHEDULE_CRON' '0 0 */6 * * *'
$justLabel    = Get-Opt 'JUST_LABEL' '0'
$processPrs   = Get-Opt 'PROCESS_PRS' '1'
$autoMerge    = Get-Opt 'AUTO_MERGE' '1'
# Precompute defaults instead of using inline if inside function call (PowerShell syntax fix)
$defaultCreate       = if ($CreateIssues) { '1' } else { Get-Opt 'CREATE_ISSUES' '0' }
$defaultCreateCount  = if ($CreateIssues) { '3' } else { Get-Opt 'CREATE_ISSUES_COUNT' '' }
$createEnv    = $defaultCreate
$createCount  = $defaultCreateCount
$useFile      = Get-Opt 'USE_FILE_FILTER' '0'

if (-not $ScheduleCron) { $ScheduleCron = $cronFromEnv }
if ($CreateIssues -and $createEnv -ne '1') { $createEnv = '1' }
#$azureApiKey = $null
#if (-not $useManagedIdentity) {
#  $azureApiKey = Require "AZURE_AI_FOUNDRY_API_KEY"
#}
if ($CreateIssues -and -not $createCount) { $createCount = '3' }


# Build app settings, conditionally include API key or managed identity flag
$settings = @(
  "GITHUB_TOKEN=$github"
  "AZURE_AI_FOUNDRY_ENDPOINT=$azureEndpoint"
  "AUTOMATION_REPOS=$repos"
  "JUST_LABEL=$justLabel"
  "PROCESS_PRS=$processPrs"
  "AUTO_MERGE=$autoMerge"
  "CREATE_ISSUES=$createEnv"
  $( if($createCount){ "CREATE_ISSUES_COUNT=$createCount" } )
  "USE_FILE_FILTER=$useFile"
  "SCHEDULE_CRON=$ScheduleCron"
)
if ($useManagedIdentity) {
    $settings += "USE_MANAGED_IDENTITY=1"
} else {
    $settings += "AZURE_AI_FOUNDRY_API_KEY=$azureApiKey"
}

Write-Host "Applying settings..." -ForegroundColor Cyan
az functionapp config appsettings set -n $FunctionAppName -g $ResourceGroup --settings $settings | Out-Null

Write-Host "Publishing code..." -ForegroundColor Cyan
func azure functionapp publish $FunctionAppName | Out-Null

Write-Host "Done." -ForegroundColor Green
Write-Host "Summary:" -ForegroundColor Cyan
Write-Host "  FunctionApp:   $FunctionAppName"
Write-Host "  Repos:         $repos"
Write-Host "  Create Issues: $createEnv (count=$createCount)"
Write-Host "  JUST_LABEL:    $justLabel"
Write-Host "  PROCESS_PRS:   $processPrs  AUTO_MERGE: $autoMerge"
Write-Host "  Schedule:      $ScheduleCron"
if ($useManagedIdentity) {
  Write-Host "  Auth:          Managed Identity" -ForegroundColor Yellow
} else {
  Write-Host "  Auth:          API Key" -ForegroundColor Yellow
}
