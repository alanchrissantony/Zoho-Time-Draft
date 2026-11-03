#!/usr/bin/env bash
# Claude Code UserPromptSubmit hook: append one activity line per prompt to
# ~/.timelog/YYYY-MM-DD.jsonl. timelog.py turns these into Zoho time logs.
#
# It must never block or fail a session: no output (UserPromptSubmit stdout is
# added to Claude's context), errors swallowed, always exit 0.

[ "${TIMELOG_DISABLE:-}" = "1" ] && exit 0

{
  dir="${TIMELOG_DIR:-$HOME/.timelog}"
  input=$(cat)

  # What you actually typed: drop IDE context blocks (<ide_opened_file>…</ide_opened_file>), and
  # skip messages Claude Code injects itself (background task results, reminders), which
  # aren't you working.
  prompt=$(jq -r '(.prompt // "") | gsub("<ide_[a-z_]+>[\\s\\S]*?</ide_[a-z_]+>"; "")
                  | gsub("^\\s+|\\s+$"; "")' <<<"$input")
  case "$prompt" in
    ""|"<task-notification"*|"<system-reminder"*|"<local-command"*) exit 0 ;;
  esac

  cwd=$(jq -r '.cwd // empty' <<<"$input")
  [ -n "$cwd" ] || cwd=$PWD
  repo_path=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null) || repo_path=$cwd
  branch=$(git -C "$repo_path" branch --show-current 2>/dev/null)

  # GitLab/GitHub group of origin, e.g. ssh://git@host:2224/codemonk/kawada/x.git → codemonk/kawada
  remote=$(git -C "$repo_path" remote get-url origin 2>/dev/null)
  path=${remote%.git}; path=${path%/}
  if [[ $path == *://* ]]; then path=${path#*://}; path=${path#*/}; else path=${path#*:}; fi
  group=""
  [[ -n $remote && $path == */* ]] && group=${path%/*}

  mkdir -p "$dir"
  jq -c \
    --arg ts "$(date +%Y-%m-%dT%H:%M:%S%z)" \
    --arg repo "$(basename "$repo_path")" \
    --arg repo_path "$repo_path" \
    --arg group "$group" \
    --arg branch "$branch" \
    --arg cwd "$cwd" \
    --arg prompt "$prompt" \
    '{ts: $ts, repo: $repo, repo_path: $repo_path, remote_group: $group, branch: $branch,
      cwd: $cwd, session_id: (.session_id // ""), prompt: $prompt[0:200]}' \
    <<<"$input" >>"$dir/$(date +%F).jsonl"
} >/dev/null 2>&1

exit 0
