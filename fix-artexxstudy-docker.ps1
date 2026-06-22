$ErrorActionPreference = "Continue"

$RepoPath = "D:\mehdi\GIT\git-gallery-runner\repos\artexxpro-artan-artexxstudy"
$ProjectName = "artexxpro-artan-artexxstudy"

Write-Host "Going to repo path..." -ForegroundColor Cyan
Set-Location $RepoPath

Write-Host "Stopping compose stack..." -ForegroundColor Cyan
docker compose down --remove-orphans

Write-Host "Finding containers related to project: $ProjectName" -ForegroundColor Cyan
$containers = docker ps -a --filter "name=$ProjectName" --format "{{.ID}} {{.Names}}"

if (-not $containers) {
    Write-Host "No project containers found." -ForegroundColor Yellow
} else {
    Write-Host "Containers found:" -ForegroundColor Yellow
    $containers

    Write-Host "Removing project containers..." -ForegroundColor Cyan
    docker ps -a --filter "name=$ProjectName" --format "{{.ID}}" | ForEach-Object {
        docker rm -f $_
    }
}

Write-Host "Starting compose stack again..." -ForegroundColor Cyan
docker compose up --build -d

Write-Host "Current compose status:" -ForegroundColor Green
docker compose ps

Write-Host "Done." -ForegroundColor Green
