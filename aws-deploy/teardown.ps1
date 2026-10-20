<#
.SYNOPSIS
    Delete everything deploy.ps1 created for Radiology Copilot.

.DESCRIPTION
    Touches only resources named with the rc- prefix (plus the ALB/target groups).
    RDS is deleted WITHOUT a final snapshot, because this is a throwaway demo
    database. That is deliberate and irreversible.

.EXAMPLE
    .\teardown.ps1 -Region us-east-1
#>
[CmdletBinding()]
param(
    [string]$Region = "us-east-1",
    [string]$Project = "rc",
    [switch]$Force
)

$ErrorActionPreference = "Continue"
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Done($m) { Write-Host "    $m" -ForegroundColor Gray }

if (-not $Force) {
    Write-Host "This will DELETE the RDS instance (no snapshot) and all ECS/ALB resources." -ForegroundColor Yellow
    $answer = Read-Host "Type 'delete' to continue"
    if ($answer -ne "delete") { Write-Host "Aborted."; return }
}

Step "ECS services"
foreach ($svc in @("$Project-api", "$Project-ui")) {
    aws ecs update-service --region $Region --cluster $Project --service $svc --desired-count 0 2>$null | Out-Null
    aws ecs delete-service --region $Region --cluster $Project --service $svc --force 2>$null | Out-Null
    Done "deleted $svc"
}
aws ecs delete-cluster --region $Region --cluster $Project 2>$null | Out-Null
Done "deleted cluster $Project"

Step "Load balancer"
$AlbArn = aws elbv2 describe-load-balancers --region $Region --names "$Project-alb" --query "LoadBalancers[0].LoadBalancerArn" --output text 2>$null
if ($AlbArn -and $AlbArn -ne "None") {
    aws elbv2 delete-load-balancer --region $Region --load-balancer-arn $AlbArn 2>$null | Out-Null
    Done "deleted ALB"
    Start-Sleep -Seconds 20
}
foreach ($tg in @("$Project-api-tg", "$Project-ui-tg")) {
    $arn = aws elbv2 describe-target-groups --region $Region --names $tg --query "TargetGroups[0].TargetGroupArn" --output text 2>$null
    if ($arn -and $arn -ne "None") { aws elbv2 delete-target-group --region $Region --target-group-arn $arn 2>$null | Out-Null; Done "deleted $tg" }
}

Step "RDS"
$DbStatus = aws rds describe-db-instances --region $Region --db-instance-identifier "$Project-mysql" --query "DBInstances[0].DBInstanceStatus" --output text 2>$null
if ($DbStatus -and $DbStatus -ne "None") {
    aws rds delete-db-instance --region $Region --db-instance-identifier "$Project-mysql" `
        --skip-final-snapshot --delete-automated-backups 2>$null | Out-Null
    Done "deleting $Project-mysql (takes a few minutes; it will bill until gone)"
}

Step "Cloud Map"
$NsId = aws servicediscovery list-namespaces --region $Region --query "Namespaces[?Name=='$Region.local'].Id" --output text 2>$null
if ($NsId -and $NsId -ne "None") {
    $svcArn = aws servicediscovery list-services --region $Region --filters "Name=NAMESPACE_ID,Values=$NsId" --query "Services[?Name=='$Project-api'].Id" --output text 2>$null
    if ($svcArn -and $svcArn -ne "None") { aws servicediscovery delete-service --region $Region --id $svcArn 2>$null | Out-Null }
    aws servicediscovery delete-namespace --region $Region --id $NsId 2>$null | Out-Null
    Done "deleted namespace"
}

Step "IAM roles"
foreach ($role in @("$Project-ecs-execution-role", "$Project-ecs-task-role")) {
    aws iam detach-role-policy --role-name $role --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy" 2>$null | Out-Null
    aws iam delete-role-policy --role-name $role --policy-name inline 2>$null | Out-Null
    aws iam delete-role --role-name $role 2>$null | Out-Null
    Done "deleted $role"
}

Step "Secrets"
foreach ($s in @("groq-api-key", "mysql-user", "mysql-password", "mysql-database", "mysql-host")) {
    aws secretsmanager delete-secret --region $Region --secret-id "$Project/$s" --force-delete-without-recovery 2>$null | Out-Null
    Done "deleted $Project/$s"
}

Step "ECR repositories"
foreach ($repo in @("$Project-api", "$Project-ui")) {
    aws ecr delete-repository --region $Region --repository-name $repo --force 2>$null | Out-Null
    Done "deleted $repo"
}

Step "Log groups"
foreach ($lg in @("/ecs/$Project-api", "/ecs/$Project-ui")) {
    aws logs delete-log-group --region $Region --log-group-name $lg 2>$null | Out-Null
    Done "deleted $lg"
}

Step "Security groups"
$VpcId = aws ec2 describe-vpcs --region $Region --filters "Name=isDefault,Values=true" --query "Vpcs[0].VpcId" --output text 2>$null
foreach ($sg in @("$Project-alb-sg", "$Project-app-sg", "$Project-db-sg")) {
    $id = aws ec2 describe-security-groups --region $Region --filters "Name=group-name,Values=$sg" "Name=vpc-id,Values=$VpcId" --query "SecurityGroups[0].GroupId" --output text 2>$null
    if ($id -and $id -ne "None") { aws ec2 delete-security-group --region $Region --group-id $id 2>$null | Out-Null; Done "deleted $sg" }
}

Step "TEARDOWN COMPLETE"
Write-Host "Verify nothing is left billing:" -ForegroundColor Yellow
Write-Host "  aws rds describe-db-instances --region $Region --query 'DBInstances[].DBInstanceIdentifier'" -ForegroundColor Gray
Write-Host "  aws elbv2 describe-load-balancers --region $Region --query 'LoadBalancers[].LoadBalancerName'" -ForegroundColor Gray
