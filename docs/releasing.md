# Releasing (for maintainers)

The release contract is small and deterministic. Follow it and a tag ships a
release; skip a step and CI stops you.

## The version is the single source of truth

The version lives in **one place**: `pyproject.toml`.

```toml
[project]
version = "X.Y.Z"
```

Nothing else defines the version. Everything downstream reads it from here.

## Steps to cut a release

1. **Bump `version`** in `pyproject.toml` to `X.Y.Z`.
2. **Add a matching changelog section.** `CHANGELOG.md` must contain a
   `## X.Y.Z` heading for that exact version. The release pipeline requires it —
   there is no release without a changelog entry.
3. **Merge to the default branch** with CI green (see below).
4. **Tag `vX.Y.Z`** and push the tag.

Tagging `vX.Y.Z` triggers **`.github/workflows/release.yml`**, which builds a
**source archive** and publishes a **GitHub release** for the tag.

## CI gates every PR

**`.github/workflows/ci.yml`** runs on pull requests and must pass to merge:

- **Leak scan** — `python backend/scrub/scan.py .`. Any **blocking** finding fails
  the job. Run it locally before pushing; it has no third-party dependencies.
- **Tests** — the test suite must pass.

Keep the tag, the `pyproject.toml` version, and the `CHANGELOG.md` heading in lock
step. If they disagree, fix the source before tagging rather than re-tagging.
