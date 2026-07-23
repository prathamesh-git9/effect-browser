param(
    [Parameter(Mandatory = $true)]
    [string]$WorktreePath,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$mutex = [System.Threading.Mutex]::new(
    $false,
    "Local\EffectBrowserAutobuild"
)
$acquired = $false

try {
    $acquired = $mutex.WaitOne(0)
    if (-not $acquired) {
        exit 0
    }

    $resolvedWorktree = (Resolve-Path -LiteralPath $WorktreePath).Path
    $gitDir = Join-Path $resolvedWorktree ".git"
    if (-not (Test-Path -LiteralPath $gitDir)) {
        throw "Worktree is not a Git checkout: $resolvedWorktree"
    }

    $completionMarker = Join-Path $resolvedWorktree "automation\AUTOMATION_COMPLETE"
    if (Test-Path -LiteralPath $completionMarker) {
        exit 0
    }

    $dirty = & git -C $resolvedWorktree status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the automation worktree"
    }
    $promptPath = Join-Path $resolvedWorktree "automation\autobuild.prompt.md"
    $prompt = Get-Content -Raw -LiteralPath $promptPath
    if ($dirty) {
        $prompt = @"
The dedicated automation worktree already contains unfinished changes from a
previous scheduled run. Inspect the diff before editing. Continue only the
same clearly identifiable roadmap slice, preserve all valid work, and never
reset, clean, or discard those changes.

$prompt
"@
    }
    $logDirectory = Join-Path $resolvedWorktree "artifacts\automation"
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $eventLog = Join-Path $logDirectory "$stamp.jsonl"
    $lastMessage = Join-Path $logDirectory "$stamp.final.txt"

    $arguments = @(
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox", "workspace-write",
        "--cd", $resolvedWorktree,
        "--output-last-message", $lastMessage,
        $prompt
    )

    if ($DryRun) {
        [pscustomobject]@{
            Worktree = $resolvedWorktree
            Prompt = $promptPath
            EventLog = $eventLog
            LastMessage = $lastMessage
            Command = "codex " + ($arguments -join " ")
        }
        exit 0
    }

    & codex @arguments 2>&1 | Tee-Object -FilePath $eventLog
    if ($LASTEXITCODE -ne 0) {
        throw "codex exec failed with exit code $LASTEXITCODE"
    }
}
finally {
    if ($acquired) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
