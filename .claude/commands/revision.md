# /revision

Address review comments on a pull request.

## Usage

```
/revision <PR number or URL>
```

## Steps

1. **Fetch comments** — use `gh pr view <PR> --comments` and `gh api repos/{owner}/{repo}/pulls/<PR>/comments` to get all review comments (both top-level PR comments and inline code comments). Also fetch any unresolved review threads with `gh api repos/{owner}/{repo}/pulls/<PR>/reviews`.

2. **Triage** — group comments into:
   - Actionable changes (bugs, style, logic feedback)
   - Questions / clarifications needed before acting
   - Nits / acknowledged (no code change required)

3. **Ask before coding** — if any comment is ambiguous or implies non-trivial design decisions, surface those questions to the user first. Wait for answers before writing code. Only proceed when there is a clear path for every actionable item.

4. **Sync with main** — before making changes, bring the branch up to date:
   - `git fetch origin main`
   - Prefer rebase: `git rebase origin/main`. If the branch has already been pushed and a rebase would require a force push, use a merge commit instead: `git merge origin/main`.
   - Push the sync commit if a merge was used: `git push`.

5. **Implement** — make the changes on the PR's branch (or worktree). Follow all project conventions:
   - Do not force-push; add new commits on top of the existing branch.
   - Keep commits focused: one logical change per commit where practical.
   - Commit messages: subject line + at most two short paragraphs. Put full details in the PR description, not the commit body.
   - Append the Co-Authored-By footer on every commit:
     ```
     Co-Authored-By: Claude $(Model) $(Version) <noreply@anthropic.com>
     ```

6. **Push** — `git push` to the remote branch (no force).

7. **Reply to comments** — after pushing, respond to every review comment and review thread on the PR:
   - For actionable comments that were addressed: briefly confirm what was done (one sentence).
   - For nits / acknowledged items: confirm you've seen it and note whether a change was made.
   - For questions you surfaced and got answers to: summarize the resolution.
   - Use `gh api repos/{owner}/{repo}/pulls/comments/<comment_id>/replies -X POST -f body="..."` for inline code comments and `gh pr comment <PR> --body "..."` for top-level PR comments.
   - Append `\n\n🤖 Generated with Claude Code` to each reply body.

8. **Summarize** — list what was changed and which comments were addressed. Note any comments intentionally left unaddressed and why.
