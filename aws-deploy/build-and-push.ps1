<#
.SYNOPSIS
    Build and push the rc-api and rc-ui images to ECR.

.DESCRIPTION
    Always builds with --platform linux/amd64 to match the ECS task definition's
    X86_64 runtime platform. Skipping this on an Apple Silicon or ARM machine
    produces images that pass a local test but die on Fargate with
    "exec format error".

.EXAMPLE
    .\build-and-push.ps1 -Region us-east-1
#>
[CmdletBinding()]
param(
    [string]$Region = "us-east-1",
    [string]$Project = "rc",
    [string]$Tag = "latest"
)

$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed or not on PATH. Install Docker Desktop, then re-run."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker daemon is not running. Start Docker Desktop, then re-run." }

$Account = (aws sts get-caller-identity --query Account --output text)
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Registry = "$Account.dkr.ecr.$Region.amazonaws.com"

Step "ECR login"
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $Registry

Step "Build + push $Project-api (linux/amd64)"
docker build --platform linux/amd64 -f (Join-Path $RepoRoot "Dockerfile.api") -t "${Registry}/${Project}-api:${Tag}" $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "API image build failed." }
docker push "${Registry}/${Project}-api:${Tag}"

Step "Build + push $Project-ui (linux/amd64)"
docker build --platform linux/amd64 -f (Join-Path $RepoRoot "Dockerfile.ui") -t "${Registry}/${Project}-ui:${Tag}" $RepoRoot
if ($LASTEXITCODE -ne 0) { throw "UI image build failed." }
docker push "${Registry}/${Project}-ui:${Tag}"

Step "DONE"
Write-Host "  Pushed:" -ForegroundColor Green
Write-Host "    ${Registry}/${Project}-api:${Tag}"
Write-Host "    ${Registry}/${Project}-ui:${Tag}"
Write-Host ""
Write-Host "  Now force a new ECS deployment:" -ForegroundColor Gray
Write-Host "    aws ecs update-service --region $Region --cluster $Project --service $Project-api --force-new-deployment" -ForegroundColor Gray
Write-Host "    aws ecs update-service --region $Region --cluster $Project --service $Project-ui  --force-new-deployment" -ForegroundColor Gray
