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
| Docker CE | `5:28.3.3-1~ubuntu.24.04~noble` `[VERIFY]` | Official apt pin; exact patch confirmed via `apt-cache madison`. |
| containerd.io | `1.7.27-1` `[VERIFY]` | Pinned alongside Docker CE. |
| docker-buildx-plugin | `0.26.1-1~ubuntu.24.04~noble` `[VERIFY]` | Pinned (needed for image builds, Phase 2). |
| docker-compose-plugin | `2.39.2-1~ubuntu.24.04~noble` `[VERIFY]` | Pinned (control plane `compose`, Phase 4). |
| NVIDIA Container Toolkit | `1.17.8-1` `[VERIFY]` | All 4 toolkit pkgs pinned to one version (NVIDIA-recommended). |
| CUDA smoke image | `nvidia/cuda:12.8.1-base-ubuntu24.04` `[VERIFY digest]` | CUDA 12.8+ covers Blackwell; `nvidia-smi` comes from the host driver. |
| Isaac Sim base | `nvcr.io/nvidia/isaac-sim:5.1.0` (LOCKED) | CLAUDE.md §5, REQ-DEPLOY-005. Tag is the locked pin. |

### `[VERIFY]` — what it means

This author-stage box has no network to the package/image registries, so exact apt
**patch** numbers and image **digests** are concrete *starting* pins. They are
confirmed/locked at the execution stage:

- **apt versions** — `require_apt_pkg_version` (in `common.sh`) checks each pin against
  `apt-cache madison` after `apt-get update`. A wrong guess fails loud and prints the
  versions the repo actually offers; lock `common.sh` to one of those and re-run.
- **image digests** — see below.

### Locking digests (optional hardening, do after first pull)

The image **tags** are valid pins today. To additionally pin by `@sha256` digest,
resolve the digest after the first pull and feed it back:

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
