# Sync HolmesGPT from upstream

Syncs are run locally because the repository does not allow GitHub Actions to
create pull requests. Start with clean local clones of both repositories, then
check out the HolmesGPT release tag you want to import:

```bash
HOLMESGPT_VERSION=0.40.0

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
helm template test ./helm >/dev/null
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

The integration patch adds Saturn defaults directly to `helm/values.yaml`. This
makes Saturn available to Arc extension and regular Helm installs, both of which
load only the primary values file by default.

If the patch fails, port the rejected Saturn hunks to the new upstream
templates, refresh `patches/saturn-integration.patch`, and rerun the script.
Do not preserve or copy entire modified upstream templates; doing so would hide
new upstream changes.

## Release after a sync

Syncing or merging the branch does not publish a release. After the sync is
merged, a pushed AKS Investigator release tag triggers the
[release workflow](../.github/workflows/release.yaml). This version is separate
from the upstream HolmesGPT version: use only unprefixed `X.Y.Z`, optionally
with a SemVer prerelease suffix. The workflow uses the tag unchanged for
chart/OCI versions and rejects `v` prefixes and `+buildmetadata`. For example,
`0.1.0` and `0.1.0-rc.1` are valid; **these are illustrative tags, not evidence
of a published release**.

Publication proceeds through native plugin checks and CPU/GPU E2E, then chart
publication to source ACR, publication and verification of the tested raw
plugin binaries in source ACR, and finally GitHub release assets. The chart's
`appVersion` still comes from [../UPSTREAM_VERSION](../UPSTREAM_VERSION).
Plugin binaries are checksum-verified and renamed, not rebuilt during
publication. Exactly three native-checked targets are required: `linux/amd64`,
`windows/amd64`, and the macOS runner's actual `darwin/arm64` or `darwin/amd64`
target, not both and not all manual cross-build targets.

Chart and plugin publication share the `AZURE_CONTAINER_REGISTRY` secret and
the managed-identity self-hosted 1ES runner. Plugin artifacts use
`<acr>.azurecr.io/public/microsoft.aks.investigator/kubectl-investigate:<version>-<goos>-<goarch>`.
Each contains one raw executable layer (`application/octet-stream`), with no
archive, checksum, license, or inventory payload. For example, source ACR
`<acr>.azurecr.io/public/microsoft.aks.investigator/kubectl-investigate:0.1.0-linux-amd64`
contains **kubectl-investigate-0.1.0-linux-amd64**. The Windows tag
`0.1.0-windows-amd64` contains **kubectl-investigate-0.1.0-windows-amd64.exe**;
`.exe` is only in the filename, not the registry tag.

The matching GitHub release retains the original Git tag; if missing, it is
created with `--verify-tag` and generated notes, marking new prereleases.
It receives only the same versioned raw binaries, replacing same-named assets
on reruns (`--clobber`). **SHA256SUMS** is internal to Actions handoffs for E2E
and release publication, not published to ACR or GitHub. No `latest` registry
tag is published. Follow the
[release binary installation instructions](../kubectl-plugin/README.md#release-binaries-when-published)
to rename downloads for kubectl discovery; Linux/macOS requires executable
permission (`chmod +x`), including after ORAS pulls. No extraction is needed.

Source ACR verification is not MCR onboarding or availability verification.
The expected MCR reference is
`mcr.microsoft.com/microsoft.aks.investigator/kubectl-investigate:<version>-<goos>-<goarch>`:
MCR drops the `public/` prefix and requires repository onboarding, a compatible
ingestion policy, and completed propagation. Existing source tags are reused
only for the same artifact type, single-layer binary digest, and filename;
different content fails. The workflow pulls by manifest digest and compares
binary bytes before GitHub upload, but does not provide a global publication
lock or registry-enforced immutability. See the
[plugin release flow](../kubectl-plugin/README.md#executable-checks-and-release-flow)
for artifact types, native targets, internal checksum verification, and permissions.