param(
    [Parameter(Mandatory = $true)][string]$RepoPath,
    [Parameter(Mandatory = $true)][string]$ProjectName,
    [Parameter(Mandatory = $true)][string[]]$ComposeFiles
)

$ErrorActionPreference = "Stop"
Set-Location $RepoPath

$args = @("compose", "-p", $ProjectName)
foreach ($file in $ComposeFiles) {
    $args += @("-f", $file)
}
$args += @("up", "--build", "-d")

& docker @args
exit $LASTEXITCODE
