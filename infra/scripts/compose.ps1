# PowerShell twin of compose.sh for Windows:  .\infra\scripts\compose.ps1 up -d --build
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\..")
docker compose --project-name warden --project-directory . --env-file .env `
  -f infra/compose/docker-compose.base.yml `
  -f infra/compose/docker-compose.model.yml `
  -f infra/compose/docker-compose.data.yml `
  -f infra/compose/docker-compose.app.yml `
  @args
exit $LASTEXITCODE
