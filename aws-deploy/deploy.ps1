<#
.SYNOPSIS
    Deploy Radiology Copilot to AWS ECS Fargate + RDS MySQL (portfolio demo).

.DESCRIPTION
    Creates: ECR repos, Secrets Manager secrets, RDS MySQL, CloudWatch log groups,
    IAM roles, an ALB with path routing, a Cloud Map namespace, and two ECS services.

    Idempotent-ish: every create is guarded by a lookup first, so a re-run after a
    partial failure picks up where it stopped rather than duplicating resources.

    NOT safe to run against a shared/production account without reading it first.
    Everything it creates is named with the rc- prefix so teardown.ps1 can find it.

.EXAMPLE
    .\deploy.ps1 -Region us-east-1 -StackPassword 'choose-a-strong-db-password'
#>
[CmdletBinding()]
param(
    [string]$Region = "us-east-1",
    [string]$Project = "rc",
    [string]$DbName = "radiology_copilot_db",
    [string]$DbUser = "copilot_user",
    [string]$DbInstanceClass = "db.t4g.micro",
    [Parameter(Mandatory = $true)][string]$StackPassword,
    [string]$GroqApiKey = ""
)

$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Info($m) { Write-Host "    $m" -ForegroundColor Gray }

# ---------------------------------------------------------------- preflight
Step "Preflight"
try { $ident = aws sts get-caller-identity --output json | ConvertFrom-Json } catch {
    throw "AWS CLI not authenticated. Run 'aws configure' or set AWS_PROFILE first."
}
$Account = $ident.Account
Info "account : $Account"
Info "identity: $($ident.Arn)"
Info "region  : $Region"
if (-not $GroqApiKey) {
    $envFile = Join-Path $PSScriptRoot "..\.env"
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*GROQ_API_KEY\s*=' } | Select-Object -First 1
        if ($line) { $GroqApiKey = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'") }
        Info "using GROQ_API_KEY from ../.env"
    }
}
if (-not $GroqApiKey) { throw "No Groq API key. Pass -GroqApiKey or put GROQ_API_KEY in ..\.env" }

$Azs = (aws ec2 describe-availability-zones --region $Region --query "AvailabilityZones[?State=='available'].ZoneName" --output json | ConvertFrom-Json)
$Az1 = $Azs[0]; $Az2 = $Azs[1]
Info "AZs: $Az1, $Az2"

# ------------------------------------------------------------------- ECR
Step "ECR repositories"
foreach ($repo in @("$Project-api", "$Project-ui")) {
    $exists = aws ecr describe-repositories --region $Region --repository-names $repo --query "repositories[0].repositoryUri" --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $exists) {
        aws ecr create-repository --region $Region --repository-name $repo --image-scanning-configuration scanOnPush=true | Out-Null
        Info "created $repo"
    } else { Info "$repo already exists" }
}
$ApiRepo = "$Account.dkr.ecr.$Region.amazonaws.com/$Project-api"
$UiRepo = "$Account.dkr.ecr.$Region.amazonaws.com/$Project-ui"

# --------------------------------------------------------- secrets manager
Step "Secrets Manager"
function Put-Secret($name, $value) {
    $arn = aws secretsmanager describe-secret --region $Region --secret-id $name --query ARN --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $arn) {
        aws secretsmanager create-secret --region $Region --name $name --secret-string $value | Out-Null
        Info "created $name"
    } else {
        aws secretsmanager put-secret-value --region $Region --secret-id $name --secret-string $value | Out-Null
        Info "updated $name"
    }
}
Put-Secret "$Project/groq-api-key" $GroqApiKey
Put-Secret "$Project/mysql-user" $DbUser
Put-Secret "$Project/mysql-password" $StackPassword
Put-Secret "$Project/mysql-database" $DbName

# ------------------------------------------------------- networking (VPC)
Step "Networking"
$VpcId = aws ec2 describe-vpcs --region $Region --filters "Name=isDefault,Values=true" --query "Vpcs[0].VpcId" --output text
if (-not $VpcId -or $VpcId -eq "None") { throw "No default VPC in $Region. Create one or adapt this script to your VPC." }
Info "default VPC: $VpcId"

$Subnets = (aws ec2 describe-subnets --region $Region --filters "Name=vpc-id,Values=$VpcId" --query "Subnets[].SubnetId" --output json | ConvertFrom-Json)
if ($Subnets.Count -lt 2) { throw "Need at least 2 subnets for an ALB." }
Info "subnets: $($Subnets.Count) available"

function Ensure-Sg($name, $desc, $vpc) {
    $id = aws ec2 describe-security-groups --region $Region --filters "Name=group-name,Values=$name" "Name=vpc-id,Values=$vpc" --query "SecurityGroups[0].GroupId" --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $id -or $id -eq "None") {
        $id = aws ec2 create-security-group --region $Region --group-name $name --description $desc --vpc-id $vpc --query GroupId --output text
        Info "created SG $name ($id)"
    } else { Info "SG $name exists ($id)" }
    return $id
}
$AlbSg = Ensure-Sg "$Project-alb-sg" "ALB for Radiology Copilot" $VpcId
$AppSg = Ensure-Sg "$Project-app-sg" "ECS tasks for Radiology Copilot" $VpcId
$DbSg = Ensure-Sg "$Project-db-sg" "RDS for Radiology Copilot" $VpcId

function Allow($sgId, $proto, $port, $sourceSg, $cidr) {
    $args = @("ec2", "authorize-security-group-ingress", "--region", $Region, "--group-id", $sgId, "--protocol", $proto, "--port", $port)
    if ($sourceSg) { $args += @("--source-group", $sourceSg) } else { $args += @("--cidr", $cidr) }
    aws @args 2>$null | Out-Null
}
Allow $AlbSg "tcp" 80 $null "0.0.0.0/0"
Allow $AppSg "tcp" 8501 $AlbSg $null
Allow $AppSg "tcp" 8000 $AlbSg $null
Allow $AppSg "tcp" 8000 $AppSg $null   # UI task -> API task via Cloud Map
Allow $DbSg  "tcp" 3306 $AppSg $null
Info "ingress rules ensured"

# ------------------------------------------------------------------- RDS
Step "RDS MySQL 8.0 (takes 5-10 minutes on first run)"
$DbId = "$Project-mysql"
$DbEndpoint = aws rds describe-db-instances --region $Region --db-instance-identifier $DbId --query "DBInstances[0].Endpoint.Address" --output text 2>$null
if ($LASTEXITCODE -ne 0 -or -not $DbEndpoint -or $DbEndpoint -eq "None") {
    Info "creating $DbId ..."
    aws rds create-db-instance `
        --region $Region `
        --db-instance-identifier $DbId `
        --db-instance-class $DbInstanceClass `
        --engine mysql --engine-version 8.0 `
        --master-username $DbUser --master-user-password $StackPassword `
        --allocated-storage 20 --storage-type gp3 `
        --db-name $DbName `
        --vpc-security-group-ids $DbSg `
        --backup-retention-period 1 `
        --no-publicly-accessible `
        --no-multi-az | Out-Null
    Info "waiting for RDS to become available (this is the slow part)..."
    aws rds wait db-instance-available --region $Region --db-instance-identifier $DbId
    $DbEndpoint = aws rds describe-db-instances --region $Region --db-instance-identifier $DbId --query "DBInstances[0].Endpoint.Address" --output text
}
Info "RDS endpoint: $DbEndpoint"
Put-Secret "$Project/mysql-host" $DbEndpoint

# ------------------------------------------------------------------ logs
Step "CloudWatch log groups"
foreach ($lg in @("/ecs/$Project-api", "/ecs/$Project-ui")) {
    aws logs create-log-group --region $Region --log-group-name $lg 2>$null | Out-Null
    Info "$lg"
}

# ------------------------------------------------------------------- IAM
Step "IAM roles"
$TrustPolicy = @'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
'@
$TrustFile = Join-Path $env:TEMP "rc-trust.json"
$TrustPolicy | Set-Content -Path $TrustFile -Encoding ascii

function Ensure-Role($name, $policyArn, $inlinePolicy) {
    $arn = aws iam get-role --role-name $name --query "Role.Arn" --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $arn) {
        aws iam create-role --role-name $name --assume-role-policy-document "file://$TrustFile" | Out-Null
        Info "created role $name"
        $arn = aws iam get-role --role-name $name --query "Role.Arn" --output text
    } else { Info "role $name exists" }
    if ($policyArn) { aws iam attach-role-policy --role-name $name --policy-arn $policyArn 2>$null | Out-Null }
    if ($inlinePolicy) {
        $f = Join-Path $env:TEMP "$name-inline.json"
        $inlinePolicy | Set-Content -Path $f -Encoding ascii
        aws iam put-role-policy --role-name $name --policy-name inline --policy-document "file://$f" | Out-Null
    }
    return $arn
}

$ExecPolicyArn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
$SecretReadPolicy = @"
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":"arn:aws:secretsmanager:${Region}:${Account}:secret:${Project}/*"}]}
"@
$ExecRoleArn = Ensure-Role "$Project-ecs-execution-role" $ExecPolicyArn $SecretReadPolicy
$TaskRoleArn = Ensure-Role "$Project-ecs-task-role" $null $null
Info "execution role: $ExecRoleArn"
Info "task role     : $TaskRoleArn"

# ---------------------------------------------------------- Cloud Map
Step "Cloud Map service discovery"
$NsId = aws servicediscovery list-namespaces --region $Region --query "Namespaces[?Name=='$Region.local'].Id" --output text 2>$null
if ($LASTEXITCODE -ne 0 -or -not $NsId -or $NsId -eq "None") {
    $op = aws servicediscovery create-private-dns-namespace --region $Region --name "$Region.local" --vpc $VpcId --query "OperationId" --output text
    Info "waiting for namespace..."
    do { Start-Sleep -Seconds 5; $ns = aws servicediscovery get-operation --region $Region --operation-id $op --query "Operation.Status" --output text } while ($ns -eq "PENDING" -or $ns -eq "SUBMITTED")
    $NsId = aws servicediscovery list-namespaces --region $Region --query "Namespaces[?Name=='$Region.local'].Id" --output text
}
Info "namespace: $NsId"

$SvcArn = aws servicediscovery list-services --region $Region --filters "Name=NAMESPACE_ID,Values=$NsId" --query "Services[?Name=='$Project-api'].Arn" --output text 2>$null
if ($LASTEXITCODE -ne 0 -or -not $SvcArn -or $SvcArn -eq "None") {
    $SvcArn = aws servicediscovery create-service --region $Region --name "$Project-api" --namespace-id $NsId `
        --dns-config "NamespaceId=$NsId,RoutingPolicy=MULTIVALUE,DnsRecords=[{Type=A,TTL=10}]" `
        --health-check-custom-config FailureThreshold=1 --query "Service.Arn" --output text
    Info "created discovery service $Project-api"
} else { Info "discovery service exists" }

# ------------------------------------------------------------------ ECS
Step "ECS cluster"
aws ecs create-cluster --region $Region --cluster-name $Project --capacity-providers FARGATE 2>$null | Out-Null
Info "cluster: $Project"

# ------------------------------------------------------------------ ALB
Step "Application Load Balancer"
$AlbArn = aws elbv2 describe-load-balancers --region $Region --names "$Project-alb" --query "LoadBalancers[0].LoadBalancerArn" --output text 2>$null
if ($LASTEXITCODE -ne 0 -or -not $AlbArn -or $AlbArn -eq "None") {
    $AlbArn = aws elbv2 create-load-balancer --region $Region --name "$Project-alb" --type application `
        --subnets $Subnets --security-groups $AlbSg --query "LoadBalancers[0].LoadBalancerArn" --output text
    Info "created ALB"
} else { Info "ALB exists" }
$AlbDns = aws elbv2 describe-load-balancers --region $Region --load-balancer-arns $AlbArn --query "LoadBalancers[0].DNSName" --output text
Info "ALB DNS: $AlbDns"

function Ensure-Tg($name, $port, $path) {
    $arn = aws elbv2 describe-target-groups --region $Region --names $name --query "TargetGroups[0].TargetGroupArn" --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $arn -or $arn -eq "None") {
        $arn = aws elbv2 create-target-group --region $Region --name $name --protocol HTTP --port $port `
            --vpc-id $VpcId --target-type ip `
            --health-check-path $path --health-check-interval-seconds 30 --health-check-timeout-seconds 10 `
            --healthy-threshold-count 2 --unhealthy-threshold-count 3 `
            --matcher "HttpCode=200" --query "TargetGroups[0].TargetGroupArn" --output text
        Info "created target group $name"
    } else { Info "target group $name exists" }
    return $arn
}
$UiTgArn = Ensure-Tg "$Project-ui-tg" 8501 "/_stcore/health"
$ApiTgArn = Ensure-Tg "$Project-api-tg" 8000 "/health"

$ListenerArn = aws elbv2 describe-listeners --region $Region --load-balancer-arn $AlbArn --query "Listeners[0].ListenerArn" --output text 2>$null
if ($LASTEXITCODE -ne 0 -or -not $ListenerArn -or $ListenerArn -eq "None") {
    $ListenerArn = aws elbv2 create-listener --region $Region --load-balancer-arn $AlbArn --protocol HTTP --port 80 `
        --default-actions "Type=forward,TargetGroupArn=$UiTgArn" --query "Listeners[0].ListenerArn" --output text
    Info "created listener :80 (default -> UI)"
} else { Info "listener exists" }

# NOTE: ALB cannot strip a path prefix, so /api/docs would 404 unless the app sets
# root_path. The API is therefore reachable at http://<alb>/health, /openapi.json etc.
# on the UI target group by default. To serve the API on its own path, either add a
# host-based rule with a DNS name, or set FastAPI root_path=/api and add the rule below.
$ruleExists = aws elbv2 describe-rules --region $Region --listener-arn $ListenerArn --query "Rules[?Priority=='10'].RuleArn" --output text 2>$null
if ($LASTEXITCODE -ne 0 -or -not $ruleExists -or $ruleExists -eq "None") {
    Info "no /api path rule added (see note in deploy.ps1 about prefix stripping)"
}

# --------------------------------------------------- task definitions
Step "Registering task definitions"
foreach ($pair in @(@("api", $ApiRepo), @("ui", $UiRepo))) {
    $which = $pair[0]
    $tpl = Get-Content (Join-Path $PSScriptRoot "taskdef-$which.json") -Raw
    $tpl = $tpl.Replace("__ACCOUNT_ID__", $Account).Replace("__REGION__", $Region)
    $f = Join-Path $env:TEMP "rc-taskdef-$which.json"
    $tpl | Set-Content -Path $f -Encoding utf8
    aws ecs register-task-definition --region $Region --cli-input-json "file://$f" | Out-Null
    Info "registered rc-$which"
}

# ---------------------------------------------------------- services
Step "ECS services"
function Ensure-Service($name, $taskFamily, $tgArn, $desired) {
    $status = aws ecs describe-services --region $Region --cluster $Project --services $name --query "services[0].status" --output text 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $status -or $status -eq "None" -or $status -eq "INACTIVE") {
        $netArgs = "awsvpcConfiguration={subnets=[$($Subnets -join ',')],securityGroups=[$AppSg],assignPublicIp=ENABLED}"
        $lbArgs = "targetGroupArn=$tgArn,containerName=$taskFamily,containerPort=$(if($taskFamily -eq 'rc-ui'){8501}else{8000})"
        aws ecs create-service --region $Region --cluster $Project --service-name $name `
            --task-definition $taskFamily --desired-count $desired --launch-type FARGATE `
            --network-configuration $netArgs --load-balancers $lbArgs `
            --service-registries "registryArn=$SvcArn" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            # Cloud Map registry may fail for the UI (already registered) -- retry without it
            aws ecs create-service --region $Region --cluster $Project --service-name $name `
                --task-definition $taskFamily --desired-count $desired --launch-type FARGATE `
                --network-configuration $netArgs --load-balancers $lbArgs | Out-Null
        }
        Info "created service $name"
    } else { Info "service $name exists ($status)" }
}
Ensure-Service "$Project-api" "$Project-api" $ApiTgArn 1
Ensure-Service "$Project-ui"  "$Project-ui"  $UiTgArn  1

Step "DONE"
Write-Host ""
Write-Host "  ALB URL : http://$AlbDns" -ForegroundColor Green
Write-Host "  API docs: http://$AlbDns/docs   (only if you add the /api rule or use the API TG directly)" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Build and push images first (see README-DEPLOY.md), otherwise tasks will fail to start." -ForegroundColor Yellow
Write-Host "  Logs: aws logs tail /ecs/$Project-api --follow --region $Region" -ForegroundColor Gray
Write-Host "  Tear down: .\teardown.ps1 -Region $Region" -ForegroundColor Gray
