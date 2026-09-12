Screenshot evidence for pull requests opened from this fork.

Kept on a branch of its own so it is never part of a PR's diff. A fork author
cannot use `gh pr edit --attach` (that needs write access to the upstream
repository), so the PR description links these files by raw URL instead. A
maintainer who prefers a `user-attachments` URL can re-attach the same file.
