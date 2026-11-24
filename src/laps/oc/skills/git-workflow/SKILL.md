---
name: git-workflow
description: Git day to day and in trouble - commits and messages, branches, rebase vs merge, resolving conflicts, stash, cherry-pick, bisect, recovering lost work with reflog, undoing safely, .gitignore/.gitattributes, LFS, submodules, hooks, and GitHub/GitLab CLI. Use for any version-control task or when a repository is in a confusing state.
keywords: git, コミット, commit, ブランチ, branch, rebase, リベース, マージ, コンフリクト, conflict, stash, cherry-pick, プルリク, pull request
---

# Git

## Before touching anything
`git status`, `git branch -vv`, `git log --oneline --graph -15 --all`, `git stash list`. Know the current branch, upstream, and whether the tree is clean. Never discard uncommitted work without being asked.

## Commits
- Small, focused commits; message: imperative summary <= 72 chars, blank line, why (not what). Match the repository's existing style (check `git log`).
- Stage deliberately (`git add -p` is interactive - in automation, add explicit paths). Check `git diff --staged` before committing.
- Never commit secrets, build outputs, or large binaries without LFS. If a secret was committed, it must be rotated - rewriting history is not enough once pushed.

## Branches and integration
- Feature branch from the up-to-date main: `git fetch origin && git switch -c feat/x origin/main`.
- Update a feature branch: `git rebase origin/main` (clean history, rewrites your commits) or `git merge origin/main` (safe for shared branches). Do not rebase branches others have pulled.
- Push a rebased branch with `git push --force-with-lease` (never plain `--force`).

## Conflicts
1. `git status` lists conflicted files. Open each, resolve `<<<<<<< ======= >>>>>>>` blocks by understanding both sides (read the commits: `git log --merge -p <file>`).
2. Lock files: regenerate instead of hand-merging (`npm install`, `uv lock`, `cargo update -w`).
3. `git add <file>` then `git rebase --continue` / `git merge --continue`. Bail out with `--abort`.
4. Build/tests after resolving.

## Undo and recover
| Situation | Command |
|---|---|
| Unstage a file | `git restore --staged f` |
| Discard local changes to a file (destructive) | `git restore f` |
| Amend last commit (not pushed) | `git commit --amend` |
| Undo last commit, keep changes | `git reset --soft HEAD~1` |
| Revert a pushed commit | `git revert <sha>` |
| Find "lost" commits after reset/rebase | `git reflog` then `git branch rescue <sha>` |
| Deleted branch | `git reflog` / `git fsck --lost-found` |
`git reset --hard` and `git clean -fd` destroy work: make a backup branch first (`git branch backup-$(date +%s)`) and only use them when asked.

## Investigation
- `git blame -w -C -L 40,60 f`, `git log -S "text" -p` (when a string appeared/disappeared), `git log --follow f`.
- `git bisect start; git bisect bad; git bisect good <sha>; git bisect run <test-cmd>` finds the commit that broke something.

## Repository files
- `.gitignore` per language plus OS/editor junk; check what is ignored with `git check-ignore -v f`.
- `.gitattributes`: `* text=auto`, `*.sh text eol=lf`, `*.bat text eol=crlf`, LFS patterns (`git lfs track "*.psd"`).
- Submodules: `git submodule update --init --recursive` after clone.

## Non-interactive use
Set `GIT_EDITOR=true` or pass `-m`; use `--no-edit` for merges; `GIT_TERMINAL_PROMPT=0` so missing credentials fail instead of hanging; `git -c core.pager=cat log` or `--no-pager`.

## Hosting CLIs
GitHub: `gh pr create --fill`, `gh pr checks`, `gh run view --log-failed`, `gh issue list`. GitLab: `glab mr create`.
