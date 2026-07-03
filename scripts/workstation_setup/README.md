# Workstation Provisioning (M5 / Phase 1)

Idempotent, version-pinned scripts that provision the GPU workstation (`etri6000`,
Ubuntu 24.04) with **Docker CE + NVIDIA Container Toolkit**, then prove GPU
passthrough (DoD-P1-02) and pull the Isaac Sim base image (DoD-P1-03).

> **The GPU driver is never touched.** The driver (R580+ open kernel module) is a
> prerequisite, asserted against a floor only. Provisioning installs the container
> stack on top.
>
> **Author stage vs. execution stage.** Authoring these files changes nothing on the
> host. Actual installation runs only after the CEO installs the sudo drop-in
> ([Step A](#step-a--ceo-installs-the-sudo-drop-in)) — then [Step B](#step-b--run-provisioning).

---

## Prerequisites (host)

- NVIDIA driver **>= 580.65.06**, open kernel module (present: 595.71.05, confirmed).
- Base tools present: `curl`, `gpg`, `dpkg` (all confirmed on `etri6000`).
- Network egress to `download.docker.com`, `nvidia.github.io`, `nvcr.io`.
- This repo (`cv-infra-workspace`) checked out on the workstation (paths below assume
  `~/cv-infra-workspace`; adjust as needed, or `scp` the single drop-in file).

---

## Step A — CEO installs the sudo drop-in

The provisioning scripts run over **non-interactive SSH**, which cannot answer a sudo
password prompt (G-06). So an operator installs a **NOPASSWD drop-in** once, in their
own terminal (decision `2026-06-25-workstation-sudo-nopasswd`, option A). Run **on the
workstation, in your own terminal**:

```bash
cd ~/cv-infra-workspace
sudo install -m 0440 -o root -g root \
  scripts/workstation_setup/sudoers.d-cv-infra /etc/sudoers.d/cv-infra
sudo visudo -cf /etc/sudoers.d/cv-infra && echo "drop-in OK"
```

`visudo -cf` must print `... parsed OK` (and `drop-in OK`). The drop-in authorizes
exactly six binaries for user `etri`; see [sudo 1:1 mapping](#sudo-11-mapping).

**Teardown (after Phase 1 — the drop-in is removable):**

```bash
sudo rm /etc/sudoers.d/cv-infra
```

---

## Step B — Run provisioning

After Step A, from the workstation (or a non-interactive SSH session):

```bash
bash scripts/workstation_setup/provision.sh
```

`provision.sh` runs, in order, each step idempotent and re-runnable:

| Step | Script | What it does | Gate |
|---|---|---|---|
| preflight | (inline) | assert OS=noble, arch=amd64, driver >= floor (read-only) | DoD-P1-01 (driver) |
| 1 | `install_docker.sh` | pinned Docker CE via official apt repo; enable service; add `etri` to docker group | — |
| 2 | `install_nvidia_toolkit.sh` | pinned NVIDIA Container Toolkit; `nvidia-ctk runtime configure`; restart docker | — |
| 3 | `test_gpu_passthrough.sh` | `docker run --rm --gpus all <cuda> nvidia-smi` -> exit 0 | **DoD-P1-02** |
| 4 | `pull_isaac.sh` | `docker pull nvcr.io/nvidia/isaac-sim:5.1.0` (+ host cache scaffold) | **DoD-P1-03** |

Individual steps can also be run on their own (e.g. `bash scripts/workstation_setup/test_gpu_passthrough.sh`).

---

## Pins (single source of truth = `common.sh`)

All version/image pins live in `common.sh`. A pin that the repo does not offer is a
**hard, loud failure** — no silent drift (reproducibility: CLAUDE.md §2-7).

| Pin | Value | Rationale |
|---|---|---|
| Driver floor | `580.65.06` (asserted) | R580 branch, Isaac 5.1 floor (NFR-DEPLOY-005). Open kernel module. |
| Docker CE | `5:28.3.3-1~ubuntu.24.04~noble` (confirmed 2026-06-26) | Official apt pin; exact patch confirmed via `apt-cache madison`. |
| containerd.io | `1.7.27-1` (confirmed 2026-06-26) | Pinned alongside Docker CE. |
| docker-buildx-plugin | `0.26.1-1~ubuntu.24.04~noble` (confirmed 2026-06-26) | Pinned (needed for image builds, Phase 2). |
| docker-compose-plugin | `2.39.2-1~ubuntu.24.04~noble` (confirmed 2026-06-26) | Pinned (control plane `compose`, Phase 4). |
| NVIDIA Container Toolkit | `1.17.8-1` (confirmed 2026-06-26) | All 4 toolkit pkgs pinned to one version (NVIDIA-recommended). |
| CUDA smoke image | `nvidia/cuda:12.8.1-base-ubuntu24.04` @ digest (locked 2026-06-26) | CUDA 12.8+ covers Blackwell; `nvidia-smi` comes from the host driver. |
| Isaac Sim base | `nvcr.io/nvidia/isaac-sim:5.1.0` @ digest (LOCKED tag; digest locked 2026-06-26) | CLAUDE.md §5, REQ-DEPLOY-005. Tag is the locked pin; digest is extra hardening. |

### Pin confirmation (execution stage, 2026-06-26 on `etri6000`)

The apt **patch** numbers and image **digests** were concrete *starting* pins authored
on a CPU box with no registry network. They are now **confirmed/locked** against the live
registries — every author-stage guess matched exactly, **no correction was needed**:

- **apt versions** — `require_apt_pkg_version` (in `common.sh`) checks each pin against
  `apt-cache madison` after `apt-get update`. All 5 Docker pins + the toolkit pin were on
  offer and installed without drift. (A wrong guess would fail loud and print the offered
  versions; lock `common.sh` to one of those and re-run.)
- **image digests** — locked from the first-pull `RepoDigests` (see below).

### Locked digests (set as defaults in `common.sh`, 2026-06-26)

Both image pins now carry their `@sha256` digest as the default (still env-overridable for
a future re-lock). Resolved from the first pull's `RepoDigests` (manifest-list digest):

```
CV_CUDA_TEST_DIGEST = sha256:133c78a0575303be34164d0b90137a042172bdf60696af01a3c424ab402d86e2
CV_ISAAC_DIGEST     = sha256:f3563cb2ba0c18af0b2fb321360dcb73a917b899f879e3213623d6bee484fa54
```

To re-resolve/re-lock a digest after a future pull, feed it back:

```bash
# CUDA smoke image
sudo docker pull nvidia/cuda:12.8.1-base-ubuntu24.04
sudo docker image inspect --format '{{index .RepoDigests 0}}' nvidia/cuda:12.8.1-base-ubuntu24.04

# Isaac Sim base
sudo docker pull nvcr.io/nvidia/isaac-sim:5.1.0
sudo docker image inspect --format '{{index .RepoDigests 0}}' nvcr.io/nvidia/isaac-sim:5.1.0
```

Take the `sha256:<...>` part and set it in `common.sh` (`CV_CUDA_TEST_DIGEST` /
`CV_ISAAC_DIGEST`), or pass it for one run, e.g.
`CV_ISAAC_DIGEST=sha256:<...> bash scripts/workstation_setup/pull_isaac.sh`. The scripts
then pull by `image@sha256:<digest>`. Until locked, they pull by tag and print a clear
warning (this is the only way to bootstrap a digest pin — not a silent fallback).

> Moving tags (a rolling `latest`-style tag, or a git-branch image ref) are forbidden
> project-wide; these scripts use exact tags/versions only.

### NGC pull fallback (DoD-P1-03)

`pull_isaac.sh` attempts an **anonymous** NGC pull. If NGC requires authentication
(rate-limit / org terms; R13), it stops and prints the fallback — run in **your own
terminal** (interactive password, G-06):

```bash
sudo docker login nvcr.io      # username: $oauthtoken   password: <NGC API key>
bash scripts/workstation_setup/pull_isaac.sh
```

---

## sudo 1:1 mapping

`sudoers.d-cv-infra` whitelists exactly the binaries the scripts call via `sudo -n`:

| Whitelisted binary | Called by | Exact invocation(s) |
|---|---|---|
| `/usr/bin/apt-get` | `install_docker.sh`, `install_nvidia_toolkit.sh` | `apt-get update`; `apt-get install -y <pinned pkgs>` |
| `/usr/bin/install` | `install_docker.sh`, `install_nvidia_toolkit.sh` | place apt keyring (`/etc/apt/keyrings/...`, `/usr/share/keyrings/...`) + `sources.list.d` files |
| `/usr/sbin/usermod` | `install_docker.sh` | `usermod -aG docker etri` |
| `/usr/bin/systemctl` | `install_docker.sh`, `install_nvidia_toolkit.sh` | `systemctl enable --now docker`; `systemctl restart docker` |
| `/usr/bin/nvidia-ctk` | `install_nvidia_toolkit.sh` | `nvidia-ctk runtime configure --runtime=docker` |
| `/usr/bin/docker` | `test_gpu_passthrough.sh`, `pull_isaac.sh` | `docker run --rm --gpus all ...`; `docker image inspect ...`; `docker pull ...` |

`curl` / `gpg` are **not** whitelisted: keys are downloaded as the user and placed with
`install`. Arguments are unconstrained (accepted, removable, window-limited scope — see
the drop-in header).

---

## Self-hosted GitHub Actions runner (M8 / DoD-P1-07)

`register_gh_runner.sh` registers this workstation as a **repo-level** self-hosted
runner for `cv-infra-workspace` (decision `2026-07-03-self-hosted-runner-policy` —
binding), labels `[self-hosted, cv-infra-gpu]`, persisted as the systemd service
**`cv-infra-gh-runner`**. Pins (runner version + official tarball sha256) live in
`common.sh` (→ *P1-07 self-hosted runner pins*). Self-update is disabled
(`--disableupdate`); GitHub's `./svc.sh` is **not** used (its sudo calls fall outside
the `/etc/sudoers.d/cv-infra` whitelist — this script only needs whitelisted
`sudo -n install` / `sudo -n systemctl`).

### Registration (token issued locally, never persisted)

The short-lived registration token is issued **locally** (where `gh` is authenticated;
the workstation has no `gh`) and piped straight into the remote environment — it must
never be committed, logged, or echoed:

```bash
# one shot, local machine:
scp scripts/workstation_setup/{common.sh,register_gh_runner.sh} cv-infra-ws:/tmp/cv-runner-setup/
gh api -X POST repos/yongjunshin/cv-infra-workspace/actions/runners/registration-token --jq .token \
  | ssh cv-infra-ws 'IFS= read -r RUNNER_REG_TOKEN; export RUNNER_REG_TOKEN
                     bash /tmp/cv-runner-setup/register_gh_runner.sh'
```

The script is **idempotent**: re-running skips the download (version marker), skips
registration (`.runner` present — no token needed for a plain re-run), and touches the
unit only on drift. Verify from any `gh`-enabled host:

```bash
gh api repos/yongjunshin/cv-infra-workspace/actions/runners \
  --jq '.runners[] | {name,status,busy,labels:[.labels[].name]}'   # expect status=online, busy=false
ssh cv-infra-ws 'systemctl is-active cv-infra-gh-runner && systemctl is-enabled cv-infra-gh-runner'
```

### Pin refresh (accepted maintenance)

GitHub may refuse jobs from runners **>30 days** behind the minimum supported version
(self-update is pinned off). To refresh: re-resolve the latest release
(`gh api repos/actions/runner/releases/latest`), update `CV_GH_RUNNER_VERSION` +
`CV_GH_RUNNER_TARBALL_SHA256` in `common.sh` (checksum from the release notes'
`BEGIN SHA linux-x64` marker), then re-run the script — it stops the service, swaps
the binaries (registration survives; `.runner` is untouched), and restarts.

### Hardening state (public repo; R10 / OD-1 / D-J)

Applied at registration time — exposure starts the moment the runner is online:

- Repo Actions fork-PR policy: **require approval for ALL outside collaborators**
  (not just first-time). Set via API/UI at registration; re-check after org/repo changes.
- **No workflow consumes the `cv-infra-gpu` label until P5** — `ci.yml` stays
  single-tier `ubuntu-latest`; the runner sits idle by design.
- GPU jobs will never check out / execute PR-head sources: the SUT enters as an
  **image ref only** (enforced at P5); `pull_request_target` is not used anywhere.

### Teardown

```bash
# local machine — issue a short-lived REMOVE token and pipe it in (never echoed):
gh api -X POST repos/yongjunshin/cv-infra-workspace/actions/runners/remove-token --jq .token \
  | ssh cv-infra-ws 'IFS= read -r T
                     sudo -n systemctl disable --now cv-infra-gh-runner
                     cd ~/cv-infra-gh-runner && ./config.sh remove --token "$T"
                     sudo -n install -m 0644 /dev/null /etc/systemd/system/cv-infra-gh-runner.service
                     sudo -n systemctl daemon-reload'
```

(`install /dev/null` neutralizes the unit within the sudo whitelist; a root shell can
`rm` the file outright. Then delete `~/cv-infra-gh-runner`.)
