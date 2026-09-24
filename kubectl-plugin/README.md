# kubectl-investigate

A `kubectl` plugin that asks the in-cluster AKS Investigator to investigate a
question about the current cluster. It is adapted from
[`mainred/investigate`](https://github.com/mainred/investigate) for this chart's
default `aks-investigator` release in the `aks-investigator` namespace.

The plugin runs `python holmes_cli.py ask` inside the Holmes deployment with
`kubectl exec`. Questions and additional Holmes CLI flags are forwarded
unchanged. Linux, macOS, and Windows use native executables built from the same
Go module, [go.mod](go.mod), with shared command handling in [main.go](main.go)
and platform execution in [exec_unix.go](exec_unix.go) and
[exec_windows.go](exec_windows.go). The former Linux/macOS Bash script and
separate Windows module have been replaced. No WSL, local Python, or local Bash
is needed to run the plugin. Timing mode alone requires Bash **inside the
Holmes container**; Python/Holmes also runs there, not on the client.

## Prerequisites

- AKS Investigator installed in the cluster.
- `kubectl` configured for that cluster.
- Permission to get deployments and exec into the Holmes pod.

## Install

Source builds require [Go](https://go.dev/dl/) **1.23 or newer**; CI uses
**1.24.x**. The shared module uses `golang.org/x/term v0.34.0` for terminal
detection and its indirect dependency `golang.org/x/sys v0.35.0`; these require
Go 1.23. A first build may need network access to download dependencies.
Go is a build-time requirement, not a plugin runtime requirement.

### Linux / macOS

From the repository root, build for the local OS/architecture and install:

```bash
go -C kubectl-plugin build -mod=readonly -trimpath -o bin/host/kubectl-investigate .
mkdir -p ~/.local/bin
install -m 0755 kubectl-plugin/bin/host/kubectl-investigate ~/.local/bin/kubectl-investigate

# Add ~/.local/bin to PATH in this session, then verify discovery.
export PATH="$HOME/.local/bin:$PATH"
kubectl plugin list | grep investigate
```

Keep that directory on `PATH` in future terminals. Any directory on `PATH`
works; a system-wide installation can use `/usr/local/bin` instead.

### Windows / PowerShell

Use **native Windows `kubectl.exe`** and **`kubectl-investigate.exe`**. Windows
kubectl launches plugins as executables: renaming a shell script or adding a
`.ps1`/`.cmd` wrapper does not make `kubectl investigate` work.
The native plugin needs no WSL, Git Bash, or PowerShell script execution policy
change. Use a regular PowerShell console or Windows Terminal, not PowerShell ISE.

With Go installed as described above, run this from the repository root in
PowerShell. The shared module produces the native architecture of your Windows
Go installation (x64 or ARM64):

```powershell
$pluginDir = Join-Path $env:LOCALAPPDATA 'kubectl-plugins'
New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null

Push-Location .\kubectl-plugin
try {
  go build -mod=readonly -trimpath -o (Join-Path $pluginDir 'kubectl-investigate.exe') .
  if ($LASTEXITCODE -ne 0) { throw 'Plugin build failed' }
} finally {
  Pop-Location
}

# Make the executable discoverable in this PowerShell session.
$env:PATH = "$pluginDir;$env:PATH"
Get-Command kubectl.exe, kubectl-investigate.exe
kubectl plugin list
```

For future terminals, add `%LOCALAPPDATA%\kubectl-plugins` to your **user Path**
through Windows Environment Variables, then open a new terminal. Go is needed
only when building; you can also copy a previously built executable for your
Windows architecture into that directory. Keep the filename exactly
`kubectl-investigate.exe` and remove or rename older same-named plugin wrappers
from `PATH` if discovery reports duplicates.

Configure `kubectl.exe` on Windows for your cluster, including any required
authentication helper such as `kubelogin`. Windows does not automatically use
a kubeconfig from WSL or your VS Code Remote SSH host.

```powershell
kubectl config use-context '<context-name>'

# Match these to the actual Helm release and namespace.
$env:INVESTIGATOR_NAMESPACE = '<namespace>'
$env:INVESTIGATOR_RELEASE = '<release-name>'

kubectl investigate 'how many pods are in the cluster' --model saturn-vllm

# Keep the conversational prompt open.
kubectl investigate 'how many pods are in the cluster' --model saturn-vllm --interactive

# Redirected output does not request a remote TTY.
kubectl investigate 'how many pods are in the cluster' --model saturn-vllm > result.txt
```

PowerShell single-quoted strings keep `$`, backticks, and other PowerShell
syntax literal in the question. For arguments containing embedded quotes
(for example JSON flags), prefer PowerShell 7.3+ with its standard native
argument passing; Windows PowerShell 5.1 has legacy native-argument quoting
limitations. The executable itself never invokes a local shell or evaluates
the question as code.

The executable passes stdin/stdout/stderr through to kubectl, enables `-it`
only when both input and output are consoles, and lets kubectl handle Ctrl+C.
It returns the child kubectl exit code; outer `kubectl investigate` dispatch
can normalize nonzero plugin exit codes. Run `kubectl-investigate.exe` directly
and inspect `$LASTEXITCODE` when the precise plugin code matters.

### Release binaries (when published)

Each successful tag-triggered release is configured to publish the same
native-checked raw executables to the source ACR and the matching
[GitHub Release](https://github.com/Azure/AKSInvestigator/releases). This
describes the workflow, not proof that any version is available: check the
selected release and platform assets before choosing this alternative to
building from source. The GitHub release uses the unprefixed Git tag unchanged;
asset names include that version and the platform.

1. Download **kubectl-investigate-\<version\>-\<goos\>-\<goarch\>**, with
  **.exe** appended on Windows. Go OS names are `linux`, `darwin` (macOS), and
  `windows`; choose only an architecture actually listed in that release.
2. Rename the downloaded file to **kubectl-investigate** (Linux/macOS) or
  **kubectl-investigate.exe** (Windows) and place it in the `PATH` directory
  used above. On Linux/macOS, set executable permission with `chmod +x`.
3. Verify discovery with `kubectl plugin list`.

No extraction is needed. Only raw executables are published: no archive,
checksum, license, or inventory files accompany them. **SHA256SUMS** is used
internally for Actions handoffs, not published to GitHub or ACR.

Downloaded binaries need **no Go installation** at runtime; native `kubectl`
and the cluster prerequisites still apply. Releases include only
the three native-checked targets described below, not every cross-build target.

#### Optional: download from an OCI registry

Each platform is published separately; this is a **binary distribution
artifact, not a runnable container image**. Its artifact type is
`application/vnd.microsoft.aks.investigator.kubectl-plugin.v1`. Its only payload
is the same versioned raw executable as GitHub, in one `application/octet-stream`
layer.

| Location | Platform artifact reference |
| --- | --- |
| Source ACR | `<acr>.azurecr.io/public/microsoft.aks.investigator/kubectl-investigate:<version>-<goos>-<goarch>` |
| Expected MCR | `mcr.microsoft.com/microsoft.aks.investigator/kubectl-investigate:<version>-<goos>-<goarch>` |

Replace the placeholders with the registry, unprefixed release version, and
an actually published native platform. For example:

| Source ACR reference | Raw file / GitHub asset |
| --- | --- |
| `<acr>.azurecr.io/public/microsoft.aks.investigator/kubectl-investigate:0.1.0-linux-amd64` | **kubectl-investigate-0.1.0-linux-amd64** |
| `<acr>.azurecr.io/public/microsoft.aks.investigator/kubectl-investigate:0.1.0-windows-amd64` | **kubectl-investigate-0.1.0-windows-amd64.exe** |

**0.1.0 is illustrative, not a claim of a released plugin version.** Windows
filenames end in **.exe**, but registry tags do not. No `latest` tag is published.

For a private source ACR, authenticate using an authorized identity (for
example, Azure CLI ACR login or ORAS registry authentication) and confirm
repository pull permission. Use `oras pull` to download the selected reference
into a new directory, preferably pinned to a manifest digest from a trusted
source rather than a mutable tag. Install the raw executable as above; ORAS
may not preserve executable permission, so use `chmod +x` on Linux/macOS.

MCR omits the source ACR's `public/` prefix. Its repository onboarding and
ingestion policy must permit this custom artifact type and binary media type,
and propagation must complete. Check the desired MCR reference is
actually available before using it. The workflow verifies **source ACR only**;
an expected MCR reference is not evidence of onboarding or availability.

### Manual cross-builds

Other architectures, including Windows ARM64, can be built manually. From the
repository root on Linux/macOS, enter the shared module and choose a target:

```bash
cd kubectl-plugin
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -mod=readonly -trimpath -o bin/linux-amd64/kubectl-investigate .
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -mod=readonly -trimpath -o bin/linux-arm64/kubectl-investigate .
GOOS=darwin GOARCH=amd64 CGO_ENABLED=0 go build -mod=readonly -trimpath -o bin/darwin-amd64/kubectl-investigate .
GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 go build -mod=readonly -trimpath -o bin/darwin-arm64/kubectl-investigate .
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -mod=readonly -trimpath -o bin/windows-amd64/kubectl-investigate.exe .
GOOS=windows GOARCH=arm64 CGO_ENABLED=0 go build -mod=readonly -trimpath -o bin/windows-arm64/kubectl-investigate.exe .
```

Build output is gitignored. Cross-compilation is not native execution testing
and does not mean all six targets are published as release assets.

## Usage

```bash
kubectl investigate --help
kubectl investigate "how many pods are in my cluster"
kubectl investigate "why is pod payment-service-1 crashlooping"
kubectl investigate "list noisy namespaces by log volume" --model saturn-vllm
```

Use `kubectl investigate --help` or `kubectl investigate -h` for local usage
and environment settings. A leading help flag prints to stdout and exits with
code 0 without deployment discovery, cluster access, or tracing credentials.
The executable also supports `kubectl-investigate --help` directly (including
`kubectl-investigate.exe --help` on Windows), even without kubectl installed.
Help flags after a question are forwarded to Holmes like other CLI options;
they are not intercepted locally. No arguments still prints usage to stderr
and exits with code 64.

When attached to a terminal, the plugin allocates a TTY so Holmes's rich output
uses the terminal width. A question runs in one-shot mode by default, so the
command exits after printing the answer. Pass `--interactive` or `-i` to remain
in Holmes's conversational prompt. The plugin also works without a TTY,
allowing output to be piped or captured.

## Configuration

The default target is deployment `aks-investigator-holmes` in namespace `aks-investigator`.
If that deployment does not exist, the plugin falls back to finding exactly one
deployment matching `app=holmes`.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `INVESTIGATOR_NAMESPACE` | `aks-investigator` | Namespace containing AKS Investigator. |
| `INVESTIGATOR_RELEASE` | `aks-investigator` | Helm release used to form `<release>-holmes`. |
| `INVESTIGATOR_DEPLOY` | _(auto)_ | Explicit deployment; skips release and label discovery. |
| `INVESTIGATOR_SELECTOR` | `app=holmes` | Fallback deployment selector. |
| `INVESTIGATOR_HOME` | `/tmp` | Writable home directory inside the Holmes pod. |

For a release named `investigator` in namespace `observability`:

```bash
INVESTIGATOR_NAMESPACE=observability INVESTIGATOR_RELEASE=investigator \
  kubectl investigate "are any workloads unhealthy?"
```

`HOME` defaults to `/tmp` because the chart runs Holmes with a read-only root
filesystem while mounting `/tmp` as writable. Holmes uses its home directory
for cached toolset state.

## Executable checks and release flow

The reusable
[binary workflow](../.github/workflows/plugin-binaries.yaml) builds and runs
the executable on GitHub-hosted native runners with Go **1.24.x**:

| Runner | Native target |
| --- | --- |
| `ubuntu-24.04` | `linux/amd64` |
| `windows-2022` | `windows/amd64` |
| `macos-15` | `darwin` with the runner's native architecture, typically `arm64` |

Artifact names use the actual `go env GOOS` and `go env GOARCH` values; macOS
architecture is not hardcoded. Only these native-checked artifacts are eligible
for release publication. The release workflow accepts either `darwin/arm64` or
`darwin/amd64` to accommodate runner architecture changes, but requires exactly
three artifacts, one per OS, not both macOS architectures. Windows also has a
PowerShell launch smoke check.

After building the host plugin as above, run the executable checks from the
shared module directory:

```bash
go run ./tools/check-binary --binary bin/host/kubectl-investigate --kubectl kubectl
```

On Windows, point `--binary` at the built Windows executable instead.
`go run` compiles the [check driver](tools/check-binary/main.go), which runs the
already-built plugin against a recording `kubectl` fixture and verifies real
`kubectl` plugin discovery/dispatch. Check execution needs no cluster, network
access, or secrets; build dependencies may still need downloading. These are
executable integration checks, **not an added unit-test suite**.

Native build artifacts contain only the tested executable and an internal
**SHA256SUMS**, with no license payload. There is no separate packaging-check
job; plugin registry publication occurs only in the release workflow.

The existing pull-request path in the
[E2E workflow](../.github/workflows/e2e-test.yaml) calls this shared workflow
for check-in. Hosted binary checks run without Azure secrets, including for
fork and Dependabot PRs; the Azure packaging/deployment jobs are skipped for
those PRs. Trusted CPU/GPU live E2E jobs download and checksum-verify the exact
tested Linux artifact, then use that binary against both deployments.

The [release workflow](../.github/workflows/release.yaml) runs on pushed tags,
not on GitHub release creation. Its publication sequence is:

1. **Validate the tag.** Accept only unprefixed `X.Y.Z`, including SemVer
  prereleases such as `X.Y.Z-rc.1`. Use the tag unchanged for the chart and
  OCI artifact version; reject `v` prefixes, `+buildmetadata`, invalid SemVer
  (including leading zeros in numeric identifiers), and versions longer
  than 110 characters.
2. **Run native checks, then CPU/GPU E2E.** The reusable binary checks above
  gate live E2E, which uses the checksum-verified Linux artifact on both
  deployments.
3. **Publish the chart to source ACR.** The chart version is the unprefixed
  tag; its `appVersion` comes from [../UPSTREAM_VERSION](../UPSTREAM_VERSION).
4. **Publish the tested plugins directly to source ACR.** Verify the binary
  checksums from the downloaded **kubectl-investigate-\<platform\>** Actions
  artifact directories and require exactly one supported native target per OS.
  Rename the three tested binaries **without rebuilding** to
  **kubectl-investigate-\<version\>-\<platform\>**, appending **.exe** for
  Windows. `oras push --registry-config` uses isolated registry credentials
  to publish each binary directly as a single `application/octet-stream` layer
  with the artifact type above. Pull each artifact by manifest digest and
  compare its binary bytes with the tested executable before GitHub upload.
5. **Publish the GitHub assets.** After source ACR verification, recheck the
  binary checksums from the internal Actions handoff. If the release for the
  original Git tag is missing,
  create it using `--verify-tag` and `--generate-notes`; newly created
  prereleases are marked `--prerelease`. Upload **only the versioned raw
  executables**, using `--clobber` for existing asset names. Internal
  **SHA256SUMS** files are not GitHub or ACR release assets.

Chart and plugin source publication use the same `AZURE_CONTAINER_REGISTRY`
secret and the managed identity of the self-hosted
`1ES.Pool=1es-aks-investigator-pool-ubuntu` runner. The identity needs registry
repository/tag listing, push, and pull permissions; failed authorization or
listing is an error, not evidence that a tag is absent. This source ACR is
distinct in configuration from `AZURE_CONTAINER_REGISTRY_E2E`.

For a platform tag already observed in source ACR, reuse requires the same
artifact type and exactly one layer with the expected binary digest and
filename annotation. Different content **fails** and requires a new version.
This is a preflight comparison plus post-publish verification,
not registry-enforced immutability or a global lock. Workflow concurrency is
keyed by the release tag; external publishers are not locked out. Only
unprefixed release tags are supported. No `latest` registry tag is created,
and MCR publication is not verified by this flow.

## Internal performance checks

`--trace` is a Holmes CLI option, not a plugin-owned flag. For internal runs
with `--trace braintrust` (or `--trace=braintrust`), the plugin requires nonempty
local `BRAINTRUST_API_KEY` and `BRAINTRUST_ORG`; either missing value exits with
code 64 before cluster access. The option is forwarded unchanged, and the two
values are passed to `kubectl exec ... -- env` only for that CLI process.
No prompts, SDK installation, or Helm updates are performed by the plugin.

All native builds use the same environment variables and exact trace matching.
Configure credentials securely in the inherited environment; do not put them
in a command, profile, or script. `INVESTIGATOR_TIMING=1` emits the exec timing
markers and uses Bash **inside the Holmes container**, never on the client.
The separate benchmark collector still targets Linux/macOS and is not made
Windows-compatible by the native plugin.

Credentials may appear in process arguments and Kubernetes audit records.
Tracing uploads investigation data to Braintrust; use an authorized organization.
See [../docs/cpu-performance.md](../docs/cpu-performance.md) for the separate
benchmark collector and required local SDK setup. That collector requires
tracing by default; its explicit `--no-braintrust` option goes before the
question and collects timing/vLLM metrics without credentials or any `--trace`
option. It is not a plugin flag, and the local SDK import remains mandatory.

Each benchmark invocation also requires Go on `PATH` to build this checkout's
plugin once before cluster access and timing. It puts the private output's
plugin directory first on `PATH` and records the binary path, SHA-256, and build
log; dependency downloads may require network access.

The separately approved [CPU resource sweep](../docs/cpu-performance.md#cpu-resource-sweep)
uses that untraced mode, temporarily upgrades the CPU Helm release, and attempts
guarded rollback; the plugin and collector alone do not make those Helm changes.
