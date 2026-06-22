param(
    [Parameter(Mandatory = $true)][string]$RepoPath,
    [Parameter(Mandatory = $true)][string]$ProjectName,
    [Parameter(Mandatory = $true)][string[]]$ComposeFiles
)

$ErrorActionPreference = "Continue"
Set-Location $RepoPath

$args = @("compose", "-p", $ProjectName)
foreach ($file in $ComposeFiles) {
    $args += @("-f", $file)
}

Write-Host "--- docker compose down --remove-orphans ---"
& docker @($args + @("down", "--remove-orphans"))

$ids = docker ps -a --filter "name=$ProjectName" -q
if ($ids) {
    Write-Host "--- removing leftover containers ---"
    & docker rm -f $ids
}

Write-Host "--- docker compose up --build -d ---"
& docker @($args + @("up", "--build", "-d"))
exit $LASTEXITCODE
