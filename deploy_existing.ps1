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

# Temporarily enable storage access for deployment
Write-Host "Preparing storage access for deployment..." -ForegroundColor Cyan

# Get the Function App's storage account name
$storageConnectionString = az functionapp config appsettings list --name $FunctionAppName --resource-group $ResourceGroup --query "[?name=='AzureWebJobsStorage'].value" -o tsv
if ($storageConnectionString -match "AccountName=([^;]+)") {
    $storageAccountName = $matches[1]
    Write-Host "  Found storage account: $storageAccountName"
    
    # Capture original network settings
    Write-Host "  Capturing original network settings..."
    $originalSettings = az storage account show --name $storageAccountName --resource-group $ResourceGroup --query "{publicNetworkAccess: publicNetworkAccess, defaultAction: networkRuleSet.defaultAction}" | ConvertFrom-Json
    $originalPublicAccess = $originalSettings.publicNetworkAccess
    $originalDefaultAction = $originalSettings.defaultAction
    
    Write-Host "  Original settings: PublicAccess=$originalPublicAccess, DefaultAction=$originalDefaultAction" -ForegroundColor Yellow
} else {
    Write-Warning "Could not extract storage account name from connection string. Deployment may fail due to storage access restrictions."
    $storageAccountName = $null
}

# Function to restore storage settings
function Restore-StorageSettings {
    param($storageAccount, $resourceGroup, $originalPublic, $originalDefault)
    if ($storageAccount) {
        Write-Host "  Restoring original storage settings..." -ForegroundColor Cyan
        try {
            if ($originalDefault -eq "Deny") {
                az storage account update --name $storageAccount --resource-group $resourceGroup --default-action Deny | Out-Null
            }
            if ($originalPublic -eq "Disabled") {
                az storage account update --name $storageAccount --resource-group $resourceGroup --public-network-access Disabled | Out-Null
            }
            Write-Host "  ✓ Storage settings restored successfully" -ForegroundColor Green
        } catch {
            Write-Warning "Failed to restore storage settings. You may need to restore manually:"
            Write-Warning "  az storage account update --name $storageAccount --resource-group $resourceGroup --default-action $originalDefault"
            Write-Warning "  az storage account update --name $storageAccount --resource-group $resourceGroup --public-network-access $originalPublic"
        }
    }
}

# Deploy with temporary storage access
Write-Host "Publishing code..." -ForegroundColor Cyan

try {
    # Temporarily enable storage access if needed
    $deploymentSuccessful = $false
    
    if ($storageAccountName) {
        # Check if we need to modify storage settings
        if ($originalPublicAccess -eq "Disabled" -or $originalDefaultAction -eq "Deny") {
            Write-Host "  Temporarily enabling storage access..." -ForegroundColor Yellow
            
            # Enable public access and allow all networks
            if ($originalDefaultAction -eq "Deny") {
                az storage account update --name $storageAccountName --resource-group $ResourceGroup --default-action Allow | Out-Null
            }
            if ($originalPublicAccess -eq "Disabled") {
                az storage account update --name $storageAccountName --resource-group $ResourceGroup --public-network-access Enabled | Out-Null
            }
            
            Write-Host "  ✓ Storage access temporarily enabled" -ForegroundColor Green
        }
    }
    
    # Attempt deployment
    Write-Host "  Deploying function app..."
    $deployResult = func azure functionapp publish $FunctionAppName 2>&1
    
    if ($LASTEXITCODE -eq 0) {
        $deploymentSuccessful = $true
        Write-Host "  ✓ Deployment completed successfully" -ForegroundColor Green
    } else {
        Write-Error "Deployment failed. Output: $deployResult"
        throw "Function app deployment failed"
    }
    
} catch {
    Write-Error "Error during deployment: $_"
    $deploymentError = $_
} finally {
    # Always restore original storage settings
    if ($storageAccountName -and ($originalPublicAccess -eq "Disabled" -or $originalDefaultAction -eq "Deny")) {
        Restore-StorageSettings $storageAccountName $ResourceGroup $originalPublicAccess $originalDefaultAction
    }
    
    # Re-throw deployment error if it occurred
    if ($deploymentError) {
        throw $deploymentError
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
if ($storageAccountName -and ($originalPublicAccess -eq "Disabled" -or $originalDefaultAction -eq "Deny")) {
    Write-Host "  Storage Access:   Temporarily modified during deployment, now restored" -ForegroundColor Yellow
} else {
    Write-Host "  Storage Access:   No modification required" -ForegroundColor Green
}
