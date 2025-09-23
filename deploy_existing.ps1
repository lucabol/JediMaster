<#
.SYNOPSIS
Configures and publishes the existing Azure Function App (timer automation) without provisioning new infrastructure.

.DESCRIPTION
Reads all configuration from .env file, applies Function App settings (AUTOMATION_REPOS, schedule, etc.), and publishes code.
Assumes the Function App and its resource group already exist.
Uses Managed Identity authentication by default for Azure AI Foundry APIs.
Automatically enables system-assigned managed identity and configures role assignments.

.EXAMPLE
pwsh ./deploy_existing.ps1

.NOTES
Requires: Azure CLI (az), Functions Core Tools (func), .env with all required configuration.
Automatically configures managed identity and role assignments for Azure AI Foundry access.
All configuration must be provided in .env file - no command line parameters supported.
#>
# No parameters - all configuration read from .env file

$ErrorActionPreference = "Stop"

# Load .env
$envPath = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path $envPath)) { throw ".env not found at $envPath" }
$dotenv = Get-Content $envPath | Where-Object { $_ -and ($_ -notmatch '^#') -and ($_ -match '=') }
$envMap = @{}
foreach($line in $dotenv){ 
    $k,$v = $line -split '=',2
    # Remove inline comments (anything after # with optional whitespace)
    $v = $v -replace '\s*#.*$', ''
    $envMap[$k.Trim()] = $v.Trim() 
}

# Helper functions
function Require($k){ if(-not $envMap.ContainsKey($k)){ throw "Missing $k in .env" }; return $envMap[$k] }
function Get-Opt($k,$default){ if($envMap.ContainsKey($k) -and $envMap[$k]){ return $envMap[$k] } return $default }


$github = Require "GITHUB_TOKEN"
$azureEndpoint = Require "AZURE_AI_FOUNDRY_ENDPOINT"

# Get deployment configuration from .env (required)
$ResourceGroup = Require "RESOURCE_GROUP"
$FunctionAppName = Require "FUNCTION_APP_NAME"

# Get optional AI resource configuration from .env
$AIResourceGroup = Get-Opt 'AI_RESOURCE_GROUP' ''
$AIResourceName = Get-Opt 'AI_RESOURCE_NAME' ''

Write-Host "=== Updating Function App ($FunctionAppName) in RG $ResourceGroup ===" -ForegroundColor Cyan

# Extract AI resource details from endpoint or use .env configuration
function Get-AIResourceInfo {
    param($endpoint, $envAIResourceGroup, $envAIResourceName)
    
    if ($envAIResourceName -and $envAIResourceGroup) {
        return @{ ResourceGroup = $envAIResourceGroup; ResourceName = $envAIResourceName }
    }
    
    # Try to extract from endpoint URL
    if ($endpoint -match 'https://([^.]+)\.cognitiveservices\.azure\.com') {
        $resourceName = $matches[1]
        # Use .env AI_RESOURCE_GROUP or fallback to function app RG
        $rg = if ($envAIResourceGroup) { $envAIResourceGroup } else { $ResourceGroup }
        return @{ ResourceGroup = $rg; ResourceName = $resourceName }
    }
    
    # Fallback - use .env values or defaults
    $rg = if ($envAIResourceGroup) { $envAIResourceGroup } else { $ResourceGroup }
    $name = if ($envAIResourceName) { $envAIResourceName } else { "jedimaster-ai" }
    
    return @{ ResourceGroup = $rg; ResourceName = $name }
}

$aiInfo = Get-AIResourceInfo $azureEndpoint $AIResourceGroup $AIResourceName

$repos        = Get-Opt 'AUTOMATION_REPOS' 'lucabol/Hello-World'
$cronFromEnv  = Get-Opt 'SCHEDULE_CRON' '0 0 */6 * * *'
$justLabel    = Get-Opt 'JUST_LABEL' '0'
$processPrs   = Get-Opt 'PROCESS_PRS' '1'
$autoMerge    = Get-Opt 'AUTO_MERGE' '1'
$createEnv    = Get-Opt 'CREATE_ISSUES' '0'
$createCount  = Get-Opt 'CREATE_ISSUES_COUNT' ''
$useFile      = Get-Opt 'USE_FILE_FILTER' '0'

$ScheduleCron = $cronFromEnv


# Build app settings for managed identity authentication
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

# Configure managed identity and role assignments
Write-Host "Configuring managed identity..." -ForegroundColor Cyan

# Enable system-assigned managed identity for the Function App
Write-Host "  Enabling system-assigned managed identity for $FunctionAppName..."
$identityResult = az functionapp identity assign --name $FunctionAppName --resource-group $ResourceGroup | ConvertFrom-Json
$principalId = $identityResult.principalId

if (-not $principalId) {
    throw "Failed to enable managed identity or retrieve principal ID"
}

Write-Host "  Principal ID: $principalId"

# Get the AI resource ID for role assignment
Write-Host "  Configuring role assignment for AI resource: $($aiInfo.ResourceName) in RG: $($aiInfo.ResourceGroup)..."
$aiResourceId = az cognitiveservices account show --name $aiInfo.ResourceName --resource-group $aiInfo.ResourceGroup --query "id" -o tsv

if (-not $aiResourceId) {
    Write-Warning "Could not find AI resource $($aiInfo.ResourceName) in resource group $($aiInfo.ResourceGroup). Please verify the resource exists and configure role assignment manually."
    Write-Warning "Manual command: az role assignment create --assignee $principalId --role 'Cognitive Services User' --scope /subscriptions/<sub-id>/resourceGroups/$($aiInfo.ResourceGroup)/providers/Microsoft.CognitiveServices/accounts/$($aiInfo.ResourceName)"
} else {
    # Assign Cognitive Services User role
    Write-Host "  Assigning 'Cognitive Services User' role..."
    $roleAssignment = az role assignment create --assignee $principalId --role "Cognitive Services User" --scope $aiResourceId 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✓ Role assignment completed successfully" -ForegroundColor Green
    } else {
        # Check if role assignment already exists
        $existingAssignment = az role assignment list --assignee $principalId --scope $aiResourceId --role "Cognitive Services User" | ConvertFrom-Json
        if ($existingAssignment.Count -gt 0) {
            Write-Host "  ✓ Role assignment already exists" -ForegroundColor Yellow
        } else {
            Write-Warning "Failed to create role assignment. Please run manually: az role assignment create --assignee $principalId --role 'Cognitive Services User' --scope $aiResourceId"
        }
    }
}

Write-Host "Applying settings..." -ForegroundColor Cyan
az functionapp config appsettings set -n $FunctionAppName -g $ResourceGroup --settings $settings | Out-Null

# Deploy function app
Write-Host "Publishing code..." -ForegroundColor Cyan

# Ensure we're in the correct directory (where host.json is located)
$projectRoot = $PSScriptRoot
Write-Host "  Project root: $projectRoot" -ForegroundColor Gray
Set-Location $projectRoot

# Verify host.json exists
if (-not (Test-Path "host.json")) {
    throw "host.json not found in $projectRoot. Cannot deploy Azure Function."
}

Write-Host "  Starting deployment to $FunctionAppName..." -ForegroundColor Yellow
Write-Host "  This may take several minutes, especially for the first deployment or when dependencies change." -ForegroundColor Yellow
Write-Host "  Common steps that may take time:" -ForegroundColor Gray
Write-Host "    - Uploading files" -ForegroundColor Gray
Write-Host "    - Installing Python packages" -ForegroundColor Gray
Write-Host "    - Deleting old .python_packages directory" -ForegroundColor Gray
Write-Host "    - Syncing triggers" -ForegroundColor Gray
Write-Host ""

# Use Start-Process to run func with real-time output and better control
$startTime = Get-Date
Write-Host "  Deployment started at: $($startTime.ToString('HH:mm:ss'))" -ForegroundColor Gray

try {
    # Run func publish with verbose output and capture both stdout and stderr
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = "func"
    $processInfo.Arguments = "azure functionapp publish $FunctionAppName --verbose"
    $processInfo.UseShellExecute = $false
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $processInfo.CreateNoWindow = $true
    $processInfo.WorkingDirectory = $projectRoot  # Explicitly set working directory for the process
    
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $processInfo
    
    # Event handlers for real-time output
    $outputBuilder = New-Object System.Text.StringBuilder
    $errorBuilder = New-Object System.Text.StringBuilder
    
    $outputHandler = {
        if ($EventArgs.Data) {
            $line = $EventArgs.Data
            Write-Host "  FUNC: $line" -ForegroundColor Gray
            [void]$outputBuilder.AppendLine($line)
            
            # Check for specific messages that indicate progress
            if ($line -match "Deleting the old \.python_packages directory") {
                Write-Host "    ► Deleting old Python packages... (this can take 2-5 minutes)" -ForegroundColor Yellow
            } elseif ($line -match "Installing dependencies") {
                Write-Host "    ► Installing Python dependencies..." -ForegroundColor Yellow
            } elseif ($line -match "Uploading") {
                Write-Host "    ► Uploading files..." -ForegroundColor Yellow
            } elseif ($line -match "Syncing triggers") {
                Write-Host "    ► Syncing triggers..." -ForegroundColor Yellow
            } elseif ($line -match "Deployment successful") {
                Write-Host "    ► Deployment completed!" -ForegroundColor Green
            }
        }
    }
    
    $errorHandler = {
        if ($EventArgs.Data) {
            Write-Host "  ERROR: $($EventArgs.Data)" -ForegroundColor Red
            [void]$errorBuilder.AppendLine($EventArgs.Data)
        }
    }
    
    Register-ObjectEvent -InputObject $process -EventName OutputDataReceived -Action $outputHandler | Out-Null
    Register-ObjectEvent -InputObject $process -EventName ErrorDataReceived -Action $errorHandler | Out-Null
    
    $process.Start()
    $process.BeginOutputReadLine()
    $process.BeginErrorReadLine()
    
    # Wait for completion with periodic status updates
    $timeoutMinutes = 15  # 15 minute timeout
    $checkIntervalSeconds = 30
    $maxWaitTime = $timeoutMinutes * 60
    $elapsedSeconds = 0
    
    while (-not $process.HasExited -and $elapsedSeconds -lt $maxWaitTime) {
        Start-Sleep -Seconds $checkIntervalSeconds
        $elapsedSeconds += $checkIntervalSeconds
        
        if ($elapsedSeconds % 120 -eq 0) {  # Every 2 minutes
            $elapsed = [math]::Round($elapsedSeconds / 60, 1)
            Write-Host "  ⏱️  Deployment still running... ($elapsed minutes elapsed)" -ForegroundColor Cyan
        }
    }
    
    if (-not $process.HasExited) {
        Write-Host "  ⚠️  Deployment taking longer than expected ($timeoutMinutes minutes). Continuing to wait..." -ForegroundColor Yellow
        $process.WaitForExit()
    }
    
    $endTime = Get-Date
    $duration = $endTime - $startTime
    
    if ($process.ExitCode -eq 0) {
        Write-Host "  ✓ Deployment completed successfully in $($duration.TotalMinutes.ToString('F1')) minutes" -ForegroundColor Green
    } else {
        $errorOutput = $errorBuilder.ToString()
        $standardOutput = $outputBuilder.ToString()
        throw "Function app deployment failed with exit code $($process.ExitCode).`nOutput: $standardOutput`nErrors: $errorOutput"
    }
    
} catch {
    $endTime = Get-Date
    $duration = $endTime - $startTime
    Write-Host "  ❌ Deployment failed after $($duration.TotalMinutes.ToString('F1')) minutes" -ForegroundColor Red
    throw "Deployment error: $_"
} finally {
    # Clean up event handlers
    Get-EventSubscriber | Where-Object { $_.SourceObject -eq $process } | Unregister-Event
    if ($process -and -not $process.HasExited) {
        $process.Kill()
    }
    if ($process) {
        $process.Dispose()
    }
}

Write-Host "Done." -ForegroundColor Green
Write-Host "Summary:" -ForegroundColor Cyan
Write-Host "  FunctionApp:      $FunctionAppName"
Write-Host "  Repos:            $repos"
Write-Host "  Create Issues:    $createEnv (count=$createCount)"
Write-Host "  JUST_LABEL:       $justLabel"
Write-Host "  PROCESS_PRS:      $processPrs  AUTO_MERGE: $autoMerge"
Write-Host "  Schedule:         $ScheduleCron"
Write-Host "  Auth:             Managed Identity" -ForegroundColor Green
Write-Host "  AI Resource:      $($aiInfo.ResourceName) (RG: $($aiInfo.ResourceGroup))" -ForegroundColor Green
