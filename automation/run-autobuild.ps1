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
    $codexExitCode = $LASTEXITCODE
    if ($codexExitCode -ne 0) {
        $usageLimited = Select-String `
            -LiteralPath $eventLog `
            -Pattern "usage limit" `
            -Quiet
        # A scheduled Codex run can also fail on a transient CLI/network error
        # without printing the quota text. The fallback is bounded and throttled,
        # so abandoning the worktree in that case is less useful than one recovery
        # attempt. Keep the flag for diagnostics in the fallback log.
        $failureReason = if ($usageLimited) { "usage limit" } else { "codex exit $codexExitCode" }

        # The user explicitly authorized Claude Code as the unattended fallback.
        # Throttle it to one bounded invocation per six hours so a persistent
        # Codex quota outage cannot create an unbounded external bill.
        $recentClaude = Get-ChildItem `
            -LiteralPath $logDirectory `
            -Filter "*.claude.txt" |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if (
            $recentClaude -and
            $recentClaude.LastWriteTime -gt (Get-Date).AddHours(-6)
        ) {
            exit 0
        }
        $claudeLog = Join-Path $logDirectory "$stamp.claude.txt"
        $allowedTools = (
            "Read,Edit,Write,Glob,Grep," +
            "Bash(git *),Bash(*python*),Bash(*pytest*)," +
            "Bash(*ruff*),Bash(node --check *)"
        )
        $claudeArguments = @(
            "-p",
            "--no-session-persistence",
            "--permission-mode", "acceptEdits",
            "--allowedTools", $allowedTools,
            "--max-budget-usd", "1.00",
            "--effort", "medium",
            $prompt
        )
        Push-Location -LiteralPath $resolvedWorktree
        try {
            Add-Content -LiteralPath $claudeLog -Value "fallback_reason=$failureReason"
            & claude @claudeArguments 2>&1 |
                Tee-Object -FilePath $claudeLog
            if ($LASTEXITCODE -ne 0) {
                throw "Claude fallback failed with exit code $LASTEXITCODE"
            }
        }
        finally {
            Pop-Location
        }
    }
}
finally {
    if ($acquired) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
