# Sync HolmesGPT from upstream

Syncs are run locally because the repository does not allow GitHub Actions to
create pull requests. Start with clean local clones of both repositories, then
check out the HolmesGPT release tag you want to import:

```bash
HOLMESGPT_VERSION=0.36.0

git -C ~/llm/holmesgpt fetch origin --tags
git -C ~/llm/holmesgpt checkout "${HOLMESGPT_VERSION}"
git -C ~/llm/holmesgpt status --short

git switch -c "sync/holmesgpt-${HOLMESGPT_VERSION}"
HOLMESGPT_DIR=~/llm/holmesgpt ./scripts/sync.sh
```

The upstream checkout must be clean so `UPSTREAM_VERSION` records a
reproducible tag and commit. After the sync, review and validate all generated
changes:

```bash
cat UPSTREAM_VERSION
git status --short
git diff --check
git diff
helm lint ./helm
helm template test ./helm -f ./helm/values-saturn.yaml >/dev/null
```

If validation passes, commit the synced chart and push the branch:

```bash
git add helm UPSTREAM_VERSION patches/saturn-integration.patch
git commit -m "chore(helm): sync HolmesGPT chart to ${HOLMESGPT_VERSION}"
git push --set-upstream origin "sync/holmesgpt-${HOLMESGPT_VERSION}"
```

`scripts/sync.sh` builds the new chart in a temporary directory, restores the
AKSInvestigator-only files listed in `KEEP_FILES`, and applies
`patches/saturn-integration.patch` to the upstream Holmes templates. It updates
`./helm/` only after the patch passes `git apply --check`, so an incompatible
upstream change cannot leave a half-synced chart.

If the patch fails, port the rejected Saturn hunks to the new upstream
templates, refresh `patches/saturn-integration.patch`, and rerun the script.
Do not preserve or copy entire modified upstream templates; doing so would hide
new upstream changes.