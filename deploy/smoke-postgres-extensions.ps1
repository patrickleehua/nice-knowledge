[CmdletBinding()]
param(
    [string]$Image = "niceknowledge-postgres:17.10-pgvector0.8.5-zhparser2e995c4",
    [string]$ExistingContainer,
    [string]$Database = "niceknowledge"
)

$ErrorActionPreference = "Stop"
$smokeSql = Join-Path $PSScriptRoot "postgres-smoke.sql"
$ephemeralContainer = $null

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

try {
    if ($ExistingContainer) {
        $container = $ExistingContainer
    }
    else {
        $container = "niceknowledge-postgres-smoke-$([Guid]::NewGuid().ToString('N').Substring(0, 10))"
        $ephemeralContainer = $container
        $extensionsSql = (Resolve-Path (Join-Path $PSScriptRoot "postgres-extensions.sql")).Path
        $rolesSql = (Resolve-Path (Join-Path $PSScriptRoot "postgres-init.sql")).Path
        Invoke-Docker run --detach --name $container `
            --env POSTGRES_PASSWORD=postgres `
            --env POSTGRES_DB=$Database `
            --mount "type=bind,source=$extensionsSql,target=/docker-entrypoint-initdb.d/00-extensions.sql,readonly" `
            --mount "type=bind,source=$rolesSql,target=/docker-entrypoint-initdb.d/10-roles.sql,readonly" `
            $Image | Out-Null

        $ready = $false
        $readyQuery = "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'niceknowledge_migrator' AND NOT rolsuper) AND (SELECT count(*) FROM pg_extension WHERE extname IN ('vector', 'pg_trgm', 'zhparser')) = 3 AND COALESCE(current_setting('zhparser.multi_short', true), '') IN ('on', 'true')"
        foreach ($attempt in 1..60) {
            $result = & docker exec --env PGPASSWORD=postgres $container psql `
                --host 127.0.0.1 `
                --username postgres `
                --dbname $Database `
                --tuples-only `
                --no-align `
                --command $readyQuery 2> $null
            if ($LASTEXITCODE -eq 0 -and ($result -join "").Trim() -eq "t") {
                $ready = $true
                break
            }
            Start-Sleep -Seconds 1
        }
        if (-not $ready) {
            Invoke-Docker logs $container
            throw "PostgreSQL did not become ready within 60 seconds"
        }
    }

    Invoke-Docker cp $smokeSql "${container}:/tmp/postgres-smoke.sql"
    Invoke-Docker exec $container psql `
        --username postgres `
        --dbname $Database `
        --file /tmp/postgres-smoke.sql
}
finally {
    if ($ephemeralContainer) {
        & docker rm --force --volumes $ephemeralContainer *> $null
    }
}
