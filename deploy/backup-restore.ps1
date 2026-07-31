#requires -Version 7.0

[CmdletBinding()]
param(
    [ValidateSet("Backup", "Verify", "BackupAndVerify")]
    [string]$Action = "BackupAndVerify",
    [string]$BackupDir,
    [string]$SourceDatabase = "niceknowledge",
    [string]$SourceBucket = "niceknowledge",
    [string]$RestoreDatabase,
    [string]$RestoreBucket,
    [string]$PostgresUser = "postgres",
    [string]$MinioDockerEndpoint = "http://minio:9000",
    [string]$MinioAccessKey = $(if ($env:MINIO_ACCESS_KEY) { $env:MINIO_ACCESS_KEY } else { "niceknowledge" }),
    [string]$MinioSecretKey = $(if ($env:MINIO_SECRET_KEY) { $env:MINIO_SECRET_KEY } else { "niceknowledge-dev-secret" }),
    [string]$MinioImage = "minio/mc:latest",
    [switch]$RetainRestoreTargets
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")

function Invoke-External {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [Parameter(Mandatory)] [string[]]$Arguments
    )

    $output = @(& $FilePath @Arguments 2>&1 | ForEach-Object { "$_" })
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE`n$($output -join "`n")"
    }
    return $output
}

function Write-JsonFile {
    param([Parameter(Mandatory)] $Value, [Parameter(Mandatory)] [string]$Path)
    $Value | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $Path -Encoding utf8NoBOM
}

function Get-PostgresContainer {
    $container = (Invoke-External docker @("compose", "ps", "-q", "postgres") | Select-Object -First 1).Trim()
    if (-not $container) { throw "The docker compose postgres service is not running." }
    return $container
}

function Get-MinIoContainer {
    $container = (Invoke-External docker @("compose", "ps", "-q", "minio") | Select-Object -First 1).Trim()
    if (-not $container) { throw "The docker compose minio service is not running." }
    return $container
}

function Get-ComposeNetwork {
    param([Parameter(Mandatory)] [string]$Container)
    $networkJson = (Invoke-External docker @("inspect", "--format", '{{json .NetworkSettings.Networks}}', $Container) | Select-Object -First 1)
    $network = ($networkJson | ConvertFrom-Json).PSObject.Properties.Name | Select-Object -First 1
    if (-not $network) { throw "Cannot determine the compose network for container $Container." }
    return $network
}

function Invoke-Postgres {
    param(
        [Parameter(Mandatory)] [string]$Container,
        [Parameter(Mandatory)] [string[]]$Arguments
    )
    return Invoke-External docker (@("exec", $Container) + $Arguments)
}

function Invoke-Mc {
    param(
        [Parameter(Mandatory)] [string]$Network,
        [Parameter(Mandatory)] [string[]]$Arguments,
        [string]$MountPath
    )

    $dockerArgs = @(
        "run", "--rm", "--network", $Network,
        "-e", "MINIO_ENDPOINT", "-e", "MINIO_ACCESS_KEY", "-e", "MINIO_SECRET_KEY"
    )
    if ($MountPath) {
        $dockerArgs += @("-v", "${MountPath}:/backup", "-w", "/backup")
    }
    $dockerArgs += @(
        "--entrypoint", "/bin/sh", $MinioImage, "-c",
        'mc alias set store "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null && exec mc "$@"',
        "mc"
    )
    return Invoke-External docker ($dockerArgs + $Arguments)
}

function Get-DatabaseInventory {
    param(
        [Parameter(Mandatory)] [string]$Container,
        [Parameter(Mandatory)] [string]$Database
    )

    $tableLines = Invoke-Postgres $Container @(
        "psql", "-X", "-U", $PostgresUser, "-d", $Database, "-At",
        "-c", "SELECT schemaname || '|' || tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public' ORDER BY schemaname, tablename;"
    )
    $tables = [ordered]@{}
    [long]$totalRows = 0
    foreach ($line in $tableLines) {
        if (-not $line.Trim()) { continue }
        $parts = $line.Split("|", 2)
        $schema = $parts[0].Replace('"', '""')
        $table = $parts[1].Replace('"', '""')
        $count = [long]((Invoke-Postgres $Container @(
            "psql", "-X", "-U", $PostgresUser, "-d", $Database, "-At",
            "-c", "SELECT count(*) FROM `"$schema`".`"$table`";"
        ) | Select-Object -First 1).Trim())
        $tables[$line] = $count
        $totalRows += $count
    }

    $extensions = @(Invoke-Postgres $Container @(
        "psql", "-X", "-U", $PostgresUser, "-d", $Database, "-At",
        "-c", "SELECT extname || '=' || extversion FROM pg_extension ORDER BY extname;"
    ) | Where-Object { $_.Trim() })

    return [ordered]@{
        database = $Database
        table_count = $tables.Count
        total_rows = $totalRows
        extensions = $extensions
        tables = $tables
    }
}

function Get-FileInventory {
    param([Parameter(Mandatory)] [string]$Root)

    $entries = @(
        Get-ChildItem -LiteralPath $Root -Recurse -File |
            Sort-Object FullName |
            ForEach-Object {
                [ordered]@{
                    path = [IO.Path]::GetRelativePath($Root, $_.FullName).Replace("\", "/")
                    bytes = $_.Length
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
    )
    [long]$totalBytes = 0
    foreach ($entry in $entries) { $totalBytes += [long]$entry["bytes"] }
    $canonical = ($entries | ForEach-Object { "$($_['path'])`t$($_['bytes'])`t$($_['sha256'])" }) -join "`n"
    $fingerprint = [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($canonical))
    ).ToLowerInvariant()
    return [ordered]@{
        object_count = $entries.Count
        total_bytes = $totalBytes
        fingerprint_sha256 = $fingerprint
        objects = $entries
    }
}

function Assert-RestoreTargetsAreIsolated {
    if ($RestoreDatabase -eq $SourceDatabase -or $RestoreDatabase -eq "niceknowledge") {
        throw "Unsafe restore database '$RestoreDatabase'. The live/source database is never a valid restore target."
    }
    if ($RestoreBucket -eq $SourceBucket -or $RestoreBucket -eq "niceknowledge") {
        throw "Unsafe restore bucket '$RestoreBucket'. The live/source bucket is never a valid restore target."
    }
    if ($RestoreDatabase -notmatch '^[a-z][a-z0-9_]{0,62}$') {
        throw "Restore database must match ^[a-z][a-z0-9_]{0,62}$."
    }
    if ($RestoreBucket -notmatch '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$') {
        throw "Restore bucket must be a valid lowercase S3 bucket name."
    }
}

function Backup-Data {
    param(
        [Parameter(Mandatory)] [string]$PostgresContainer,
        [Parameter(Mandatory)] [string]$Network
    )

    if (Test-Path -LiteralPath $BackupDir) {
        throw "Backup directory already exists: $BackupDir"
    }
    $dbDir = New-Item -ItemType Directory -Path (Join-Path $BackupDir "database") -Force
    $objectDir = New-Item -ItemType Directory -Path (Join-Path $BackupDir "object-store\objects") -Force
    $dumpPath = Join-Path $dbDir "niceknowledge.dump"
    $globalsPath = Join-Path $dbDir "globals-no-passwords.sql"
    $schemaPath = Join-Path $dbDir "schema.sql"
    $containerDump = "/tmp/niceknowledge-$RunId.dump"
    $containerGlobals = "/tmp/niceknowledge-$RunId-globals.sql"
    $containerSchema = "/tmp/niceknowledge-$RunId-schema.sql"

    try {
        Invoke-Postgres $PostgresContainer @(
            "pg_dump", "-U", $PostgresUser, "-d", $SourceDatabase,
            "--format=custom", "--compress=zstd:6", "--file=$containerDump"
        ) | Out-Null
        Invoke-Postgres $PostgresContainer @(
            "pg_dumpall", "-U", $PostgresUser, "--globals-only", "--no-role-passwords", "--file=$containerGlobals"
        ) | Out-Null
        Invoke-Postgres $PostgresContainer @(
            "pg_dump", "-U", $PostgresUser, "-d", $SourceDatabase, "--schema-only",
            "--no-owner", "--no-privileges", "--restrict-key=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "--file=$containerSchema"
        ) | Out-Null
        Invoke-Postgres $PostgresContainer @("pg_restore", "--list", $containerDump) | Out-Null
        Invoke-External docker @("cp", "${PostgresContainer}:${containerDump}", $dumpPath) | Out-Null
        Invoke-External docker @("cp", "${PostgresContainer}:${containerGlobals}", $globalsPath) | Out-Null
        Invoke-External docker @("cp", "${PostgresContainer}:${containerSchema}", $schemaPath) | Out-Null
    }
    finally {
        Invoke-Postgres $PostgresContainer @("rm", "-f", $containerDump, $containerGlobals, $containerSchema) | Out-Null
    }

    $sourceDbInventory = Get-DatabaseInventory $PostgresContainer $SourceDatabase
    Write-JsonFile $sourceDbInventory (Join-Path $dbDir "source-inventory.json")

    $versioning = Invoke-Mc $Network @("version", "info", "--json", "store/$SourceBucket")
    $versioning | Set-Content -LiteralPath (Join-Path $BackupDir "object-store\versioning.json") -Encoding utf8NoBOM
    if (($versioning -join "`n") -match '"status"\s*:\s*"Enabled"') {
        throw "Versioning is enabled on $SourceBucket. A latest-version mirror is not a full backup; use MinIO site replication."
    }

    Invoke-Mc $Network @(
        "mirror", "--overwrite", "--remove", "--retry", "--preserve",
        "store/$SourceBucket", "/backup/object-store/objects"
    ) $BackupDir | Out-Null
    $sourceStats = Invoke-Mc $Network @("stat", "--recursive", "--json", "store/$SourceBucket")
    $sourceStats | Set-Content -LiteralPath (Join-Path $BackupDir "object-store\source-stats.jsonl") -Encoding utf8NoBOM
    $sourceListing = Invoke-Mc $Network @("ls", "--recursive", "--json", "store/$SourceBucket")
    $sourceListing | Set-Content -LiteralPath (Join-Path $BackupDir "object-store\source-listing.jsonl") -Encoding utf8NoBOM
    Invoke-Mc $Network @("admin", "cluster", "bucket", "export", "store/$SourceBucket") $BackupDir | Out-Null

    $objectInventory = Get-FileInventory $objectDir.FullName
    $listedObjects = @($sourceListing | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.type -eq "file" })
    [long]$listedBytes = ($listedObjects | Measure-Object -Property size -Sum).Sum
    $listedCount = [long]$listedObjects.Count
    $downloadedCount = [long]$objectInventory.object_count
    $downloadedBytes = [long]$objectInventory.total_bytes
    if ($listedCount -ne $downloadedCount -or $listedBytes -ne $downloadedBytes) {
        throw "MinIO source listing ($listedCount objects/$listedBytes bytes) does not match the downloaded backup ($downloadedCount objects/$downloadedBytes bytes)."
    }
    Write-JsonFile $objectInventory (Join-Path $BackupDir "object-store\source-inventory.json")
    $metadataArchive = Get-ChildItem -LiteralPath $BackupDir -Filter "*metadata.zip" -File -Recurse | Select-Object -First 1

    $manifest = [ordered]@{
        format_version = 1
        run_id = $RunId
        created_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        source = [ordered]@{ database = $SourceDatabase; bucket = $SourceBucket }
        artifacts = [ordered]@{
            database_dump = "database/niceknowledge.dump"
            database_dump_sha256 = (Get-FileHash $dumpPath -Algorithm SHA256).Hash.ToLowerInvariant()
            globals = "database/globals-no-passwords.sql"
            globals_sha256 = (Get-FileHash $globalsPath -Algorithm SHA256).Hash.ToLowerInvariant()
            schema = "database/schema.sql"
            schema_sha256 = (Get-FileHash $schemaPath -Algorithm SHA256).Hash.ToLowerInvariant()
            object_root = "object-store/objects"
            object_fingerprint_sha256 = $objectInventory.fingerprint_sha256
            bucket_metadata = $(if ($metadataArchive) { [IO.Path]::GetRelativePath($BackupDir, $metadataArchive.FullName).Replace("\", "/") } else { $null })
            bucket_metadata_sha256 = $(if ($metadataArchive) { (Get-FileHash $metadataArchive.FullName -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null })
        }
        database = $sourceDbInventory
        object_store = [ordered]@{
            object_count = $objectInventory.object_count
            total_bytes = $objectInventory.total_bytes
            versioning_enabled = $false
        }
        verification = $null
    }
    Write-JsonFile $manifest (Join-Path $BackupDir "manifest.json")
    return $manifest
}

function Verify-Restore {
    param(
        [Parameter(Mandatory)] [string]$PostgresContainer,
        [Parameter(Mandatory)] [string]$Network
    )

    Assert-RestoreTargetsAreIsolated
    $manifestPath = Join-Path $BackupDir "manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Missing backup manifest: $manifestPath" }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    $dumpPath = Join-Path $BackupDir $manifest.artifacts.database_dump
    $objectRoot = Join-Path $BackupDir $manifest.artifacts.object_root
    $restoreDownload = Join-Path $BackupDir "verification\restored-objects"
    New-Item -ItemType Directory -Path $restoreDownload -Force | Out-Null

    $actualDumpHash = (Get-FileHash $dumpPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualDumpHash -ne $manifest.artifacts.database_dump_sha256) {
        throw "Database dump checksum mismatch before restore."
    }
    $actualObjectInventory = Get-FileInventory $objectRoot
    if ($actualObjectInventory.fingerprint_sha256 -ne $manifest.artifacts.object_fingerprint_sha256) {
        throw "Object backup checksum mismatch before restore."
    }

    $containerDump = "/tmp/niceknowledge-$RunId-restore.dump"
    $containerSchema = "/tmp/niceknowledge-$RunId-restored-schema.sql"
    $restoredSchemaPath = Join-Path $BackupDir "verification\restored-schema.sql"
    $dbCreated = $false
    $bucketCreated = $false
    try {
        Invoke-Postgres $PostgresContainer @("createdb", "-U", $PostgresUser, "-T", "template0", $RestoreDatabase) | Out-Null
        $dbCreated = $true
        Invoke-External docker @("cp", $dumpPath, "${PostgresContainer}:${containerDump}") | Out-Null
        Invoke-Postgres $PostgresContainer @(
            "pg_restore", "-U", $PostgresUser, "-d", $RestoreDatabase, "--exit-on-error", $containerDump
        ) | Out-Null

        $restoredDbInventory = Get-DatabaseInventory $PostgresContainer $RestoreDatabase
        $sourceTableJson = $manifest.database.tables | ConvertTo-Json -Compress
        $restoreTableJson = $restoredDbInventory.tables | ConvertTo-Json -Compress
        if ($sourceTableJson -ne $restoreTableJson) { throw "Restored database table row counts differ from the backup manifest." }
        Invoke-Postgres $PostgresContainer @(
            "pg_dump", "-U", $PostgresUser, "-d", $RestoreDatabase, "--schema-only",
            "--no-owner", "--no-privileges", "--restrict-key=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "--file=$containerSchema"
        ) | Out-Null
        Invoke-External docker @("cp", "${PostgresContainer}:${containerSchema}", $restoredSchemaPath) | Out-Null
        $sourceSchemaHash = $manifest.artifacts.schema_sha256
        $restoredSchemaHash = (Get-FileHash $restoredSchemaPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($sourceSchemaHash -ne $restoredSchemaHash) { throw "Restored database schema checksum differs from the source schema checksum." }

        Invoke-Mc $Network @("mb", "store/$RestoreBucket") | Out-Null
        $bucketCreated = $true
        Invoke-Mc $Network @(
            "mirror", "--overwrite", "--remove", "--retry", "--preserve",
            "/backup/object-store/objects", "store/$RestoreBucket"
        ) $BackupDir | Out-Null
        $restoredStats = Invoke-Mc $Network @("stat", "--recursive", "--json", "store/$RestoreBucket")
        $restoredStats | Set-Content -LiteralPath (Join-Path $BackupDir "verification\restored-stats.jsonl") -Encoding utf8NoBOM
        Invoke-Mc $Network @(
            "mirror", "--overwrite", "--remove", "--retry", "--preserve",
            "store/$RestoreBucket", "/backup/verification/restored-objects"
        ) $BackupDir | Out-Null
        $restoredObjectInventory = Get-FileInventory $restoreDownload
        if ($restoredObjectInventory.fingerprint_sha256 -ne $manifest.artifacts.object_fingerprint_sha256) {
            throw "Restored object content fingerprint differs from the backup fingerprint."
        }

        $manifest.verification = [ordered]@{
            verified_at_utc = (Get-Date).ToUniversalTime().ToString("o")
            restore_database = $RestoreDatabase
            restore_bucket = $RestoreBucket
            database_table_count = $restoredDbInventory.table_count
            database_total_rows = $restoredDbInventory.total_rows
            database_schema_sha256 = $restoredSchemaHash
            object_count = $restoredObjectInventory.object_count
            object_total_bytes = $restoredObjectInventory.total_bytes
            object_fingerprint_sha256 = $restoredObjectInventory.fingerprint_sha256
            status = "passed"
            cleanup = $(if ($RetainRestoreTargets) { "retained by request" } else { "pending automatic cleanup" })
        }
        Write-JsonFile $restoredDbInventory (Join-Path $BackupDir "verification\restored-database-inventory.json")
        Write-JsonFile $restoredObjectInventory (Join-Path $BackupDir "verification\restored-object-inventory.json")
    }
    finally {
        Invoke-Postgres $PostgresContainer @("rm", "-f", $containerDump, $containerSchema) | Out-Null
        if (-not $RetainRestoreTargets) {
            if ($dbCreated) {
                Invoke-Postgres $PostgresContainer @("dropdb", "-U", $PostgresUser, "--if-exists", "--force", $RestoreDatabase) | Out-Null
            }
            if ($bucketCreated) {
                Invoke-Mc $Network @("rb", "--force", "store/$RestoreBucket") | Out-Null
            }
            if ($manifest.verification) { $manifest.verification.cleanup = "completed" }
        }
        Write-JsonFile $manifest $manifestPath
    }
    return $manifest
}

Push-Location $ProjectRoot
try {
    $env:MINIO_ENDPOINT = $MinioDockerEndpoint
    $env:MINIO_ACCESS_KEY = $MinioAccessKey
    $env:MINIO_SECRET_KEY = $MinioSecretKey
    $postgresContainer = Get-PostgresContainer
    $minioContainer = Get-MinIoContainer
    $network = Get-ComposeNetwork $minioContainer

    if (-not $BackupDir) {
        $BackupDir = Join-Path $ProjectRoot ".local\backups\$RunId"
    }
    $BackupDir = [IO.Path]::GetFullPath($BackupDir)
    if (-not $RestoreDatabase) { $RestoreDatabase = "niceknowledge_restore_$($RunId.ToLowerInvariant().Replace('-', '').Replace(':', ''))" }
    if (-not $RestoreBucket) { $RestoreBucket = "niceknowledge-restore-$($RunId.ToLowerInvariant())" }

    if ($Action -in @("Backup", "BackupAndVerify")) {
        Write-Host "Creating backup at $BackupDir"
        $null = Backup-Data $postgresContainer $network
    }
    if ($Action -in @("Verify", "BackupAndVerify")) {
        Write-Host "Restoring into isolated targets $RestoreDatabase and $RestoreBucket"
        $result = Verify-Restore $postgresContainer $network
        Write-Host "Restore verification: $($result.verification.status)"
    }
    Write-Host "Manifest: $(Join-Path $BackupDir 'manifest.json')"
}
finally {
    Pop-Location
}
