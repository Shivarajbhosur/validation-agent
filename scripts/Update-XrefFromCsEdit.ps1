param(
    [Parameter(Mandatory = $false)]
    [string]$RepoRoot,

    [Parameter(Mandatory = $false)]
    [string]$CsFile,

    [Parameter(Mandatory = $false)]
    [string]$XrfFile,

    [Parameter(Mandatory = $false)]
    [string]$BaseRef = "HEAD",

    [Parameter(Mandatory = $false)]
    [switch]$DryRun,

    [Parameter(Mandatory = $false)]
    [switch]$ForceRebuildIfInvalid
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Info([string]$message) {
    Write-Host "[xref] $message"
}

function Resolve-RepoRoot([string]$providedRoot) {
    if ($providedRoot -and (Test-Path -LiteralPath $providedRoot)) {
        return (Resolve-Path -LiteralPath $providedRoot).Path
    }

    $gitRoot = git rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -eq 0 -and $gitRoot) {
        return $gitRoot.Trim()
    }

    throw "Unable to resolve git repo root. Pass -RepoRoot explicitly."
}

function Get-CandidateXrfPaths([string]$csAbsPath) {
    $dir = Split-Path -Path $csAbsPath -Parent
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($csAbsPath)

    # SSAB.OX has two common layouts:
    # 1) conversion/<SG>/cs_xxx/<Name>.cs with sibling <Name>.xrf
    # 2) project-root/Xref/<Name>.xrf for compiled projects
    return @(
        (Join-Path $dir ("$baseName.xrf")),
        (Join-Path (Join-Path $dir "Xref") ("$baseName.xrf")),
        (Join-Path (Join-Path (Split-Path -Path $dir -Parent) "Xref") ("$baseName.xrf"))
    )
}

function Resolve-XrfForCs([string]$csAbsPath, [string]$xrfPath) {
    if ($xrfPath) {
        if (-not (Test-Path -LiteralPath $xrfPath)) {
            throw ".xrf file not found: $xrfPath"
        }
        return (Resolve-Path -LiteralPath $xrfPath).Path
    }

    foreach ($candidate in (Get-CandidateXrfPaths -csAbsPath $csAbsPath)) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "Could not infer .xrf for $csAbsPath. Pass -XrfFile explicitly."
}

function Resolve-FilePair([string]$root, [string]$csPath, [string]$xrfPath) {
    if ($csPath) {
        if (-not (Test-Path -LiteralPath $csPath)) {
            throw "C# file not found: $csPath"
        }

        $resolvedCs = (Resolve-Path -LiteralPath $csPath).Path
        $resolvedXrf = Resolve-XrfForCs -csAbsPath $resolvedCs -xrfPath $xrfPath
        return @(@{ Cs = $resolvedCs; Xrf = $resolvedXrf })
    }

    # No CsFile provided: use git-changed .cs files and infer .xrf locations.
    Push-Location $root
    try {
        $changed = @(git status --porcelain -- "*.cs")
        if ($LASTEXITCODE -ne 0) {
            throw "git status failed while listing changed .cs files."
        }

        $pairs = @()
        foreach ($line in $changed) {
            if (-not $line) { continue }
            if ($line.Length -lt 4) { continue }
            $rel = $line.Substring(3).Trim()
            if (-not $rel.EndsWith(".cs", [StringComparison]::OrdinalIgnoreCase)) { continue }

            $absCs = Join-Path $root $rel
            if (-not (Test-Path -LiteralPath $absCs)) { continue }

            $resolvedCs = (Resolve-Path -LiteralPath $absCs).Path
            $resolvedXrf = $null
            foreach ($candidate in (Get-CandidateXrfPaths -csAbsPath $resolvedCs)) {
                if (Test-Path -LiteralPath $candidate) {
                    $resolvedXrf = (Resolve-Path -LiteralPath $candidate).Path
                    break
                }
            }

            if (-not $resolvedXrf) { continue }
            $pairs += @{ Cs = $resolvedCs; Xrf = $resolvedXrf }
        }

        return $pairs
    }
    finally {
        Pop-Location
    }
}

function ConvertTo-XrfInt([string]$value, [string]$fieldName, [int]$lineNo) {
    $result = 0
    if (-not [int]::TryParse($value, [ref]$result)) {
        throw "Invalid integer '$value' in field $fieldName at .xrf line $lineNo"
    }
    return $result
}

function Read-Xrf([string]$xrfPath) {
    $rawLines = @()
    if (Test-Path -LiteralPath $xrfPath) {
        $rawLines = @(Get-Content -LiteralPath $xrfPath)
    }

    if (-not $rawLines -or $rawLines.Count -eq 0) {
        return @{ Valid = $false; Reason = "empty"; Rows = @() }
    }

    $rows = New-Object System.Collections.Generic.List[object]
    $lineNo = 0
    foreach ($line in $rawLines) {
        $lineNo += 1
        if ([string]::IsNullOrWhiteSpace($line)) {
            return @{ Valid = $false; Reason = "blank line at $lineNo"; Rows = @() }
        }

        $parts = $line.Split(',')
        if ($parts.Length -lt 8) {
            return @{ Valid = $false; Reason = "expected >=8 CSV fields at line $lineNo"; Rows = @() }
        }

        try {
            $targetLine = ConvertTo-XrfInt $parts[0] "targetLine" $lineNo
            $targetColumn = ConvertTo-XrfInt $parts[1] "targetColumn" $lineNo
            $length = ConvertTo-XrfInt $parts[2] "length" $lineNo
            $origTgtLineNum = ConvertTo-XrfInt $parts[3] "origTgtLineNum" $lineNo
            $origTgtColumn = ConvertTo-XrfInt $parts[4] "origTgtColumn" $lineNo
            $srcLineNum = ConvertTo-XrfInt $parts[5] "srcLineNum" $lineNo
            $srcColumn = ConvertTo-XrfInt $parts[6] "srcColumn" $lineNo
            $srcFile = ($parts[7..($parts.Length - 1)] -join ',')

            $rows.Add([pscustomobject]@{
                TargetLine = $targetLine
                TargetColumn = $targetColumn
                Length = $length
                OrigTargetLine = $origTgtLineNum
                OrigTargetColumn = $origTgtColumn
                SrcLine = $srcLineNum
                SrcColumn = $srcColumn
                SrcFile = $srcFile
            })
        }
        catch {
            return @{ Valid = $false; Reason = $_.Exception.Message; Rows = @() }
        }
    }

    return @{ Valid = $true; Reason = "ok"; Rows = $rows }
}

function Build-LineMapper([string]$root, [string]$csAbsPath, [string]$baseRef) {
    Push-Location $root
    try {
        $rel = [System.IO.Path]::GetRelativePath($root, $csAbsPath)
        $escapedRel = $rel -replace '\\','/'
        $baseBlob = "$baseRef`:$escapedRel"

        $old = @(git show $baseBlob 2>$null)
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to read baseline file from git ref '$baseRef' for $rel"
        }

        $new = @(Get-Content -LiteralPath $csAbsPath)

        $oldCount = $old.Count
        $newCount = $new.Count
        $dp = New-Object 'int[,]' ($oldCount + 1), ($newCount + 1)

        for ($i = $oldCount - 1; $i -ge 0; $i--) {
            for ($j = $newCount - 1; $j -ge 0; $j--) {
                if ($old[$i] -ceq $new[$j]) {
                    $dp[$i, $j] = 1 + $dp[($i + 1), ($j + 1)]
                }
                else {
                    $a = $dp[($i + 1), $j]
                    $b = $dp[$i, ($j + 1)]
                    $dp[$i, $j] = if ($a -ge $b) { $a } else { $b }
                }
            }
        }

        $exactMap = @{}
        $iPtr = 0
        $jPtr = 0
        while ($iPtr -lt $oldCount -and $jPtr -lt $newCount) {
            if ($old[$iPtr] -ceq $new[$jPtr]) {
                $exactMap[$iPtr + 1] = $jPtr + 1
                $iPtr += 1
                $jPtr += 1
                continue
            }

            if ($dp[($iPtr + 1), $jPtr] -ge $dp[$iPtr, ($jPtr + 1)]) {
                $iPtr += 1
            }
            else {
                $jPtr += 1
            }
        }

        return @{ ExactMap = $exactMap; NewLineCount = $newCount }
    }
    finally {
        Pop-Location
    }
}

function Update-XrfRows([object[]]$rows, [hashtable]$exactMap, [int]$newLineCount) {
    $updated = New-Object System.Collections.Generic.List[object]
    $knownOld = @($exactMap.Keys | Sort-Object)

    foreach ($row in $rows) {
        $oldLine = [int]$row.TargetLine
        $newLine = $null

        if ($exactMap.ContainsKey($oldLine)) {
            $newLine = [int]$exactMap[$oldLine]
        }
        else {
            $prev = $null
            $next = $null

            foreach ($k in $knownOld) {
                if ($k -lt $oldLine) { $prev = $k; continue }
                if ($k -gt $oldLine) { $next = $k; break }
            }

            if ($null -ne $prev -and $null -ne $next) {
                $prevNew = [int]$exactMap[$prev]
                $nextNew = [int]$exactMap[$next]
                $spanOld = [int]$next - [int]$prev
                $spanNew = [int]$nextNew - [int]$prevNew
                $offsetOld = [int]$oldLine - [int]$prev
                if ($spanOld -eq 0) {
                    $newLine = $prevNew
                }
                else {
                    $newLine = $prevNew + [math]::Round(($offsetOld * $spanNew) / $spanOld)
                }
            }
            elseif ($null -ne $prev) {
                $delta = [int]$oldLine - [int]$prev
                $newLine = [int]$exactMap[$prev] + $delta
            }
            elseif ($null -ne $next) {
                $delta = [int]$next - [int]$oldLine
                $newLine = [int]$exactMap[$next] - $delta
            }
            else {
                $newLine = $oldLine
            }
        }

        if ($newLine -lt 1) { $newLine = 1 }
        if ($newLine -gt $newLineCount) { $newLine = $newLineCount }

        $updated.Add([pscustomobject]@{
            TargetLine = $newLine
            TargetColumn = [int]$row.TargetColumn
            Length = [int]$row.Length
            OrigTargetLine = [int]$row.OrigTargetLine
            OrigTargetColumn = [int]$row.OrigTargetColumn
            SrcLine = [int]$row.SrcLine
            SrcColumn = [int]$row.SrcColumn
            SrcFile = [string]$row.SrcFile
        })
    }

    return $updated
}

function Write-Xrf([string]$xrfPath, [object[]]$rows, [switch]$dryRun) {
    $lines = @(foreach ($r in $rows) {
        "{0},{1},{2},{3},{4},{5},{6},{7}" -f $r.TargetLine, $r.TargetColumn, $r.Length, $r.OrigTargetLine, $r.OrigTargetColumn, $r.SrcLine, $r.SrcColumn, $r.SrcFile
    })

    if ($dryRun) {
        Write-Info "Dry run: would write $($lines.Count) rows to $xrfPath"
        return
    }

    Set-Content -LiteralPath $xrfPath -Value $lines
}

function Reset-MinimalXrf([string]$csAbsPath, [string]$xrfPath, [switch]$dryRun) {
    $bootstrap = "1,0,1,1,0,1,0,$csAbsPath"

    if ($dryRun) {
        Write-Info "Dry run: would rebuild minimal .xrf with bootstrap row for $xrfPath"
        return
    }

    Set-Content -LiteralPath $xrfPath -Value $bootstrap
}

$root = Resolve-RepoRoot -providedRoot $RepoRoot
$pairs = @(Resolve-FilePair -root $root -csPath $CsFile -xrfPath $XrfFile)

if (-not $pairs -or $pairs.Count -eq 0) {
    Write-Info "No changed .cs/.xrf pairs found. Nothing to do."
    exit 0
}

Write-Info "Found $($pairs.Count) candidate pair(s)."

foreach ($pair in $pairs) {
    $csAbs = $pair.Cs
    $xrfAbs = $pair.Xrf

    Write-Info "Processing C#: $csAbs"
    Write-Info "Using XRF: $xrfAbs"

    $parsed = Read-Xrf -xrfPath $xrfAbs
    if (-not $parsed.Valid) {
        Write-Info "Existing .xrf invalid ($($parsed.Reason))."
        if ($ForceRebuildIfInvalid) {
            Reset-MinimalXrf -csAbsPath $csAbs -xrfPath $xrfAbs -dryRun:$DryRun
            continue
        }
        else {
            throw ".xrf is invalid. Re-run with -ForceRebuildIfInvalid to recreate from scratch."
        }
    }

    $mapResult = Build-LineMapper -root $root -csAbsPath $csAbs -baseRef $BaseRef
    $updatedRows = Update-XrfRows -rows $parsed.Rows -exactMap $mapResult.ExactMap -newLineCount $mapResult.NewLineCount

    Write-Xrf -xrfPath $xrfAbs -rows $updatedRows -dryRun:$DryRun
    Write-Info "Updated $($updatedRows.Count) rows."
}

Write-Info "Done."
