# Branch check — is the work really on main?

A task that finishes with "built, waiting for the release" is only waiting if its work is on main or on a release-train branch.
Work that exists only on a local branch is lost when the work tree is cleaned up, and a later release does not contain it.
`scripts/branch-check/branch_check.py` answers the question with git alone (standard library, read-only).

```
python3 scripts/branch-check/branch_check.py <clone> --branch task/42 --commit 3f65d01
python3 scripts/branch-check/branch_check.py <clone> --text final-answer.md --json
```

| state | meaning | exit |
|---|---|---|
| `in_tag` | on main and in the newest `v*` tag | 0 |
| `on_main` | on main, in a release-train branch (`origin/*train*`, `origin/release/*`), or the same patch on main under another SHA; the tag is still to come | 0 |
| `not_pushed` | branch or commit only local, or only on an unrelated remote branch: **not on main** | 3 |
| `unclear` | nothing resolvable named, or git cannot run | 2 |

- Branch names decide when named (worst answer wins). Without a branch the **newest** resolvable commit decides, so
  work-in-progress commits that were squashed later do not raise a false alarm.
- `--text` extracts `task/...` and `work/...` branches (add more with `--prefix`) and SHAs (7-40 hex with at least one digit
  and one letter, so dates and plain numbers are ignored).
- `--main` names the main ref (default `origin/main`); run `git fetch` first if the answer must be current.
- A task rule that goes with it: a task that waits for a release names its branch and commit in the final answer and pushes the
  branch to the remote first.
- Tests: `python3 -m unittest discover -s scripts/branch-check`.
