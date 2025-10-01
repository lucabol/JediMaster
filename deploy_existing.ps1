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
if (-not (Test-Path $envPath)) { 
    Write-Host "❌ DEPLOYMENT FAILED: .env file not found at $envPath" -ForegroundColor Red
    Write-Host "Please create a .env file with required configuration." -ForegroundColor Yellow
    throw ".env not found at $envPath" 
}

try {
    $dotenv = Get-Content $envPath | Where-Object { $_ -and ($_ -notmatch '^#') -and ($_ -match '=') }
    $envMap = @{}
    foreach($line in $dotenv){ 
        $k,$v = $line -split '=',2
        # Remove inline comments (anything after # with optional whitespace)
        $v = $v -replace '\s*#.*$', ''
        $envMap[$k.Trim()] = $v.Trim() 
    }
} catch {
    Write-Host "❌ DEPLOYMENT FAILED: Error reading .env file" -ForegroundColor Red
    Write-Host "Error details: $_" -ForegroundColor Red
    throw "Failed to load .env file: $_"
}

# Helper functions
function Require($k){ 
    if(-not $envMap.ContainsKey($k)){ 
        Write-Host "❌ DEPLOYMENT FAILED: Missing required configuration" -ForegroundColor Red
        Write-Host "Missing variable: $k" -ForegroundColor Yellow
        Write-Host "Please add $k=<value> to your .env file" -ForegroundColor Yellow
        throw "Missing $k in .env" 
    }
    if([string]::IsNullOrWhiteSpace($envMap[$k])){
        Write-Host "❌ DEPLOYMENT FAILED: Empty required configuration" -ForegroundColor Red
        Write-Host "Variable $k is present but empty in .env file" -ForegroundColor Yellow
        throw "$k cannot be empty in .env"
    }
    return $envMap[$k] 
}
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
$mergeRetries = Get-Opt 'MERGE_MAX_RETRIES' '3'

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
  "MERGE_MAX_RETRIES=$mergeRetries"
)

# Configure managed identity and role assignments
Write-Host "Configuring managed identity..." -ForegroundColor Cyan

# Enable system-assigned managed identity for the Function App
Write-Host "  Enabling system-assigned managed identity for $FunctionAppName..."
try {
    $identityResult = az functionapp identity assign --name $FunctionAppName --resource-group $ResourceGroup 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ DEPLOYMENT FAILED: Could not enable managed identity" -ForegroundColor Red
        Write-Host "Azure CLI Error: $identityResult" -ForegroundColor Red
        Write-Host "Possible causes:" -ForegroundColor Yellow
        Write-Host "  - Function App '$FunctionAppName' does not exist in resource group '$ResourceGroup'" -ForegroundColor Yellow
        Write-Host "  - Insufficient permissions to modify the Function App" -ForegroundColor Yellow
        Write-Host "  - Azure CLI not logged in or subscription not set" -ForegroundColor Yellow
        throw "Failed to enable managed identity: $identityResult"
    }
    $identityResult = $identityResult | ConvertFrom-Json
    $principalId = $identityResult.principalId
} catch {
    Write-Host "❌ DEPLOYMENT FAILED: Error enabling managed identity" -ForegroundColor Red
    Write-Host "Error details: $_" -ForegroundColor Red
    throw "Failed to enable managed identity: $_"
}

if (-not $principalId) {
    Write-Host "❌ DEPLOYMENT FAILED: Principal ID not found" -ForegroundColor Red
    Write-Host "Managed identity was created but principal ID is empty" -ForegroundColor Red
    throw "Failed to enable managed identity or retrieve principal ID"
}

Write-Host "  Principal ID: $principalId"

# Get the AI resource ID for role assignment
Write-Host "  Configuring role assignment for AI resource: $($aiInfo.ResourceName) in RG: $($aiInfo.ResourceGroup)..."
try {
    $aiResourceId = az cognitiveservices account show --name $aiInfo.ResourceName --resource-group $aiInfo.ResourceGroup --query "id" -o tsv 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "⚠️  AI resource lookup failed" -ForegroundColor Yellow
        Write-Host "Azure CLI Error: $aiResourceId" -ForegroundColor Red
        $aiResourceId = $null
    }
} catch {
    Write-Host "⚠️  Error looking up AI resource" -ForegroundColor Yellow
    Write-Host "Error details: $_" -ForegroundColor Red
    $aiResourceId = $null
}

if (-not $aiResourceId) {
    Write-Host "⚠️  Could not find AI resource $($aiInfo.ResourceName) in resource group $($aiInfo.ResourceGroup)" -ForegroundColor Yellow
    Write-Host "     Please verify the resource exists and configure role assignment manually." -ForegroundColor Yellow
    Write-Host "     Manual command: az role assignment create --assignee $principalId --role 'Cognitive Services User' --scope /subscriptions/<sub-id>/resourceGroups/$($aiInfo.ResourceGroup)/providers/Microsoft.CognitiveServices/accounts/$($aiInfo.ResourceName)" -ForegroundColor Gray
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
try {
    $settingsResult = az functionapp config appsettings set -n $FunctionAppName -g $ResourceGroup --settings $settings 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ DEPLOYMENT FAILED: Could not apply function app settings" -ForegroundColor Red
        Write-Host "Azure CLI Error: $settingsResult" -ForegroundColor Red
        Write-Host "Attempted to set $($settings.Count) settings on $FunctionAppName" -ForegroundColor Yellow
        throw "Failed to apply settings: $settingsResult"
    }
    Write-Host "  ✓ Applied $($settings.Count) settings successfully" -ForegroundColor Green
} catch {
    Write-Host "❌ DEPLOYMENT FAILED: Error applying function app settings" -ForegroundColor Red
    Write-Host "Error details: $_" -ForegroundColor Red
    throw "Failed to apply settings: $_"
}

# Deploy function app
Write-Host "Publishing code..." -ForegroundColor Cyan

# Ensure we're in the correct directory (where host.json is located)
$projectRoot = $PSScriptRoot
Write-Host "  Project root: $projectRoot" -ForegroundColor Gray

try {
    Set-Location $projectRoot
} catch {
    Write-Host "❌ DEPLOYMENT FAILED: Cannot access project directory" -ForegroundColor Red
    Write-Host "Directory: $projectRoot" -ForegroundColor Red
    Write-Host "Error details: $_" -ForegroundColor Red
    throw "Cannot access project directory: $_"
}

# Verify host.json exists
if (-not (Test-Path "host.json")) {
    Write-Host "❌ DEPLOYMENT FAILED: Missing host.json" -ForegroundColor Red
    Write-Host "Expected location: $projectRoot\host.json" -ForegroundColor Red
    Write-Host "Current directory contents:" -ForegroundColor Yellow
    Get-ChildItem -Name | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }
    throw "host.json not found in $projectRoot. Cannot deploy Azure Function."
}

# Verify function_app.py exists
if (-not (Test-Path "function_app.py")) {
    Write-Host "❌ DEPLOYMENT FAILED: Missing function_app.py" -ForegroundColor Red
    Write-Host "Expected location: $projectRoot\function_app.py" -ForegroundColor Red
    throw "function_app.py not found in $projectRoot. Cannot deploy Azure Function."
}

# Verify requirements.txt exists
if (-not (Test-Path "requirements.txt")) {
    Write-Host "⚠️  Warning: requirements.txt not found" -ForegroundColor Yellow
    Write-Host "  Python dependencies may not be installed correctly" -ForegroundColor Yellow
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
        
        Write-Host ""
        Write-Host "❌ DEPLOYMENT FAILED: Function app deployment failed" -ForegroundColor Red
        Write-Host "Exit code: $($process.ExitCode)" -ForegroundColor Red
        Write-Host "Duration: $($duration.TotalMinutes.ToString('F1')) minutes" -ForegroundColor Red
        Write-Host ""
        
        if ($errorOutput.Trim()) {
            Write-Host "ERROR OUTPUT:" -ForegroundColor Red
            Write-Host "$errorOutput" -ForegroundColor Red
            Write-Host ""
        }
        
        if ($standardOutput.Trim()) {
            Write-Host "STANDARD OUTPUT:" -ForegroundColor Yellow
            Write-Host "$standardOutput" -ForegroundColor Gray
            Write-Host ""
        }
        
        # Try to provide helpful guidance based on common error patterns
        if ($errorOutput -match "unauthorized|authentication|login") {
            Write-Host "💡 TROUBLESHOOTING SUGGESTION:" -ForegroundColor Cyan
            Write-Host "  Authentication issue detected. Try:" -ForegroundColor Yellow
            Write-Host "  az login" -ForegroundColor Gray
            Write-Host "  az account set --subscription <your-subscription-id>" -ForegroundColor Gray
        } elseif ($errorOutput -match "not found|does not exist") {
            Write-Host "💡 TROUBLESHOOTING SUGGESTION:" -ForegroundColor Cyan
            Write-Host "  Resource not found. Verify:" -ForegroundColor Yellow
            Write-Host "  - Function App name: $FunctionAppName" -ForegroundColor Gray
            Write-Host "  - Resource Group: $ResourceGroup" -ForegroundColor Gray
            Write-Host "  - Subscription is correct" -ForegroundColor Gray
        } elseif ($errorOutput -match "requirements.txt|dependencies|pip") {
            Write-Host "💡 TROUBLESHOOTING SUGGESTION:" -ForegroundColor Cyan
            Write-Host "  Python dependency issue detected. Check:" -ForegroundColor Yellow
            Write-Host "  - requirements.txt syntax" -ForegroundColor Gray
            Write-Host "  - Package versions compatibility" -ForegroundColor Gray
            Write-Host "  - Network connectivity for package downloads" -ForegroundColor Gray
        }
        
        throw "Function app deployment failed with exit code $($process.ExitCode).`nSee error details above."
    }
    
} catch {
    $endTime = Get-Date
    $duration = $endTime - $startTime
    Write-Host ""
    Write-Host "❌ DEPLOYMENT FAILED: Unexpected error during deployment" -ForegroundColor Red
    Write-Host "Duration: $($duration.TotalMinutes.ToString('F1')) minutes" -ForegroundColor Red
    Write-Host "Error details: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "💡 TROUBLESHOOTING:" -ForegroundColor Cyan
    Write-Host "  1. Check if 'func' command is available: func --version" -ForegroundColor Yellow
    Write-Host "  2. Verify Azure Functions Core Tools installation" -ForegroundColor Yellow
    Write-Host "  3. Check network connectivity" -ForegroundColor Yellow
    Write-Host "  4. Verify Azure CLI authentication: az account show" -ForegroundColor Yellow
    Write-Host ""
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

# Verify deployment completed successfully
Write-Host "Verifying deployment..." -ForegroundColor Cyan

# 1. Check function app status
Write-Host "  Checking function app status..." -ForegroundColor Yellow
try {
    $appStatus = az functionapp show --name $FunctionAppName --resource-group $ResourceGroup --query "{state:state,hostName:defaultHostName,lastModified:lastModifiedTimeUtc}" -o json 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ⚠️  Could not retrieve function app status" -ForegroundColor Yellow
        Write-Host "     Error: $appStatus" -ForegroundColor Red
    } else {
        $statusObj = $appStatus | ConvertFrom-Json
        Write-Host "  ✓ Function App Status: $($statusObj.state)" -ForegroundColor Green
        Write-Host "    Host: $($statusObj.hostName)" -ForegroundColor Gray
        Write-Host "    Last Modified: $($statusObj.lastModified)" -ForegroundColor Gray
        
        if ($statusObj.state -ne "Running") {
            Write-Host "  ⚠️  Function app is not in Running state" -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "  ⚠️  Error checking function app status: $_" -ForegroundColor Yellow
}

# 2. Check function list and timer trigger
Write-Host "  Checking deployed functions..." -ForegroundColor Yellow
try {
    $functions = az functionapp function list --name $FunctionAppName --resource-group $ResourceGroup --query "[].{name:name,triggerType:triggerType,status:status}" -o json 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ⚠️  Could not retrieve function list" -ForegroundColor Yellow
        Write-Host "     Error: $functions" -ForegroundColor Red
    } else {
        $functionsObj = $functions | ConvertFrom-Json
        if ($functionsObj.Count -eq 0) {
            Write-Host "  ❌ No functions found in the app!" -ForegroundColor Red
            Write-Host "     This indicates the deployment may not have completed properly." -ForegroundColor Red
        } else {
            Write-Host "  ✓ Found $($functionsObj.Count) function(s):" -ForegroundColor Green
            foreach ($func in $functionsObj) {
                $statusColor = if ($func.status -eq "Enabled") { "Green" } else { "Yellow" }
                Write-Host "    - $($func.name) ($($func.triggerType)) - Status: $($func.status)" -ForegroundColor $statusColor
            }
            
            # Check for timer trigger specifically
            $timerFunctions = $functionsObj | Where-Object { $_.triggerType -eq "timerTrigger" }
            if ($timerFunctions.Count -eq 0) {
                Write-Host "  ⚠️  No timer trigger functions found" -ForegroundColor Yellow
            } else {
                Write-Host "  ✓ Timer trigger functions configured correctly" -ForegroundColor Green
            }
        }
    }
} catch {
    Write-Host "  ⚠️  Error checking functions: $_" -ForegroundColor Yellow
}

# 3. Verify application settings were applied
Write-Host "  Verifying application settings..." -ForegroundColor Yellow
try {
    $currentSettings = az functionapp config appsettings list --name $FunctionAppName --resource-group $ResourceGroup --query "[?name=='AUTOMATION_REPOS' || name=='SCHEDULE_CRON' || name=='GITHUB_TOKEN'].{name:name,value:value}" -o json 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ⚠️  Could not retrieve application settings" -ForegroundColor Yellow
        Write-Host "     Error: $currentSettings" -ForegroundColor Red
    } else {
        $settingsObj = $currentSettings | ConvertFrom-Json
        $foundSettings = @{}
        foreach ($setting in $settingsObj) {
            $foundSettings[$setting.name] = $setting.value
        }
        
        # Check key settings
        if ($foundSettings.ContainsKey('AUTOMATION_REPOS')) {
            Write-Host "  ✓ AUTOMATION_REPOS: $($foundSettings['AUTOMATION_REPOS'])" -ForegroundColor Green
        } else {
            Write-Host "  ❌ AUTOMATION_REPOS setting not found" -ForegroundColor Red
        }
        
        if ($foundSettings.ContainsKey('SCHEDULE_CRON')) {
            Write-Host "  ✓ SCHEDULE_CRON: $($foundSettings['SCHEDULE_CRON'])" -ForegroundColor Green
        } else {
            Write-Host "  ⚠️  SCHEDULE_CRON setting not found (using default)" -ForegroundColor Yellow
        }
        
        if ($foundSettings.ContainsKey('GITHUB_TOKEN')) {
            $tokenLength = $foundSettings['GITHUB_TOKEN'].Length
            $maskedToken = "*" * [Math]::Max(0, $tokenLength - 4) + $foundSettings['GITHUB_TOKEN'].Substring([Math]::Max(0, $tokenLength - 4))
            Write-Host "  ✓ GITHUB_TOKEN: $maskedToken" -ForegroundColor Green
        } else {
            Write-Host "  ❌ GITHUB_TOKEN setting not found" -ForegroundColor Red
        }
    }
} catch {
    Write-Host "  ⚠️  Error checking application settings: $_" -ForegroundColor Yellow
}

# 4. Test the reset endpoint to verify the function app is responding
Write-Host "  Testing function app responsiveness..." -ForegroundColor Yellow
try {
    $hostKey = az functionapp keys list --name $FunctionAppName --resource-group $ResourceGroup --query "functionKeys.default" -o tsv 2>$null
    if ($hostKey -and $LASTEXITCODE -eq 0) {
        $testUrl = "https://$FunctionAppName.azurewebsites.net/api/reset?code=$hostKey"
        
        # Quick HEAD request to check if the endpoint responds (don't actually reset)
        $response = try {
            Invoke-WebRequest -Uri $testUrl -Method HEAD -TimeoutSec 10 -ErrorAction Stop
            "SUCCESS"
        } catch {
            if ($_.Exception.Message -match "405" -or $_.Exception.Message -match "Method Not Allowed") {
                # 405 is expected for HEAD on POST endpoint - means the app is responding
                "SUCCESS"
            } else {
                $_.Exception.Message
            }
        }
        
        if ($response -eq "SUCCESS") {
            Write-Host "  ✓ Function app is responding to requests" -ForegroundColor Green
        } else {
            Write-Host "  ⚠️  Function app response test failed: $response" -ForegroundColor Yellow
            Write-Host "     This might be normal if the app is still warming up." -ForegroundColor Gray
        }
    } else {
        Write-Host "  ⚠️  Could not retrieve function key for testing" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  ⚠️  Error testing function app: $_" -ForegroundColor Yellow
}

# 5. Check recent deployment logs for any errors
Write-Host "  Checking recent deployment logs..." -ForegroundColor Yellow
try {
    $recentLogs = az functionapp logs tail --name $FunctionAppName --resource-group $ResourceGroup --timeout 5 2>$null
    if ($recentLogs -and $LASTEXITCODE -eq 0) {
        $errorLines = $recentLogs | Where-Object { $_ -match "(error|exception|failed)" -and $_ -notmatch "INFO" }
        if ($errorLines.Count -eq 0) {
            Write-Host "  ✓ No recent errors found in logs" -ForegroundColor Green
        } else {
            Write-Host "  ⚠️  Found $($errorLines.Count) potential error(s) in recent logs:" -ForegroundColor Yellow
            $errorLines | Select-Object -First 3 | ForEach-Object {
                Write-Host "     $_" -ForegroundColor Red
            }
        }
    } else {
        Write-Host "  ⚠️  Could not retrieve recent logs" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  ⚠️  Error checking logs: $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "✓ Deployment verification completed" -ForegroundColor Green
Write-Host ""

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

Write-Host ""
Write-Host "💡 Next steps to verify your deployment:" -ForegroundColor Cyan
Write-Host "  1. Monitor logs: az functionapp logs tail --name $FunctionAppName --resource-group $ResourceGroup" -ForegroundColor Gray
Write-Host "  2. Check portal: https://portal.azure.com/#@/resource/subscriptions/<subscription>/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$FunctionAppName" -ForegroundColor Gray
Write-Host "  3. Test reset endpoint: POST https://$FunctionAppName.azurewebsites.net/api/reset?code=<function-key>" -ForegroundColor Gray
Write-Host "  4. Wait for timer trigger: Next run according to '$ScheduleCron'" -ForegroundColor Gray
