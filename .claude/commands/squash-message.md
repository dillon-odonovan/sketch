# /squash-message

Generate the squash-merge commit message for a pull request.

## Usage

```
/squash-message <PR number or URL>
```

## Steps

1. **Gather context** — run:
   - `gh pr view <PR>` — title, body, and labels
   - `gh log main..<branch> --oneline` — all commits that will be squashed
   - `gh pr diff <PR>` (or `git diff main...<branch>`) — full diff for change summary

2. **Draft the message** following these rules:
   - **Subject line**: conventional-commit prefix + concise description, e.g. `feat(add-team): accept VRPaste URLs`. Match the style of recent commits on `main` (`git log main --oneline -10`).
   - **Body**: one paragraph on *why* the change was made; optionally a second paragraph for non-obvious mechanics. No bullet lists, no exhaustive change inventory — those belong in the PR description.
   - **Footer**:
     ```
     Co-Authored-By: Claude $(Model) $(Version) <noreply@anthropic.com>
     ```

3. **Output** the full message in a fenced code block so it can be copy-pasted into GitHub's "Squash and merge" dialog.
