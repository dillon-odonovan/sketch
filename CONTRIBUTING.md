# Contributing to Sketch

## Filing issues

Open issues at <https://github.com/dillon-odonovan/sketch/issues/new/choose>. Four templates:

- **Feature request** — new capability or enhancement. Stay product-focused; implementation gets decided when the work is picked up.
- **Bug report** — something is broken or behaving unexpectedly.
- **Task / Chore** — small work items that aren't a feature or bug (infra, refactor, dependency bump).
- **Idea** — half-formed thoughts you don't want to lose. Promote to a feature request later if it's worth doing.

Required fields are kept to the minimum needed to make the issue understandable later. Skip anything optional you don't have an answer for.

Starting a Claude Code session on something new? File an issue first using whichever template matches the work — a feature, a bug, a chore, or an idea you're about to flesh out.

## Submitting changes

1. Branch off `main`.
2. Open a PR — the template prompts for a summary, the linked issue, and a test plan.
3. Wait for CI (`ci.yml`) to pass.
4. Use GitHub's **Squash and merge** button. Author commits with the `Co-Authored-By: Claude <noreply@anthropic.com>` footer where applicable.

## Local setup

See the `Setup` section of [README.md](README.md).
