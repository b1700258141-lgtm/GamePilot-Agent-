$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$upload = Join-Path $repo 'artifacts/modelscope-upload'
New-Item -ItemType Directory -Force -Path $upload | Out-Null

# Only committed files enter the archive; local .env and uncommitted work stay local.
& git -C $repo archive --format=tar.gz -o (Join-Path $upload 'modelscope-source.payload') HEAD
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }

# An earlier .tar.gz upload was stored as a Git LFS pointer by ModelScope.
$oldArchive = Join-Path $upload 'modelscope-source.tar.gz'
if (Test-Path -LiteralPath $oldArchive) { Remove-Item -LiteralPath $oldArchive }

Copy-Item -LiteralPath (Join-Path $repo 'deploy/modelscope/Dockerfile') -Destination $upload -Force
Copy-Item -LiteralPath (Join-Path $repo 'ms_deploy.json') -Destination $upload -Force
Write-Output "Upload the three files in $upload to the ModelScope Studio, then deploy Docker on Free CPU."
