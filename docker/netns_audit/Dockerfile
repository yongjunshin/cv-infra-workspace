# docker/netns_audit/Dockerfile — egress-audit SIDECAR image (NEG-1 runtime half, DoD-P5-05).
#
# WHY THIS FILE EXISTS: the image `scripts/netns_audit.sh` runs had no recipe in the
# repository — only a two-line sketch inside that script's header comment — so the audit half
# of a deployment could not be REGENERATED. Two consequences, both measured: a fresh host
# comes up with NO sidecar, and a plane that cannot be audited is not a plane that audits
# clean (the runtime NEG-1 observation is simply unavailable there); and a reader with no file
# to open GUESSED the image name from a sibling script's convention, which put two false
# propositions into a report before anyone noticed (G-86). This file is that recipe.
#
# WHAT IT IS: a throwaway container carrying the distro `iptables` binary and nothing else.
# `netns_audit.sh` runs it with `--network container:<target> --cap-add NET_ADMIN`, so it
# ENTERS the audited container's network namespace and installs (`arm`) or reads (`read`) the
# counter-only netfilter rules there. Do-not-reinvent (M5 §2): stock kernel counters through
# the distro binary — no capture daemon, no pcap store, no observation agent. It runs as root
# (the base default) because manipulating the target netns needs it; the capability, not the
# image, is what the script grants.
#
# WHY IT STAYS THIS SMALL: it shares the netns of the process under audit, so anything this
# image does on the network is counted BY THE VERY CHAIN it installs. Hence one apt layer, no
# runtime downloads, and `read` calling `iptables -n` (numeric — a reverse lookup would be DNS
# traffic charged to the audited namespace).
#
# BUILD — the context is irrelevant (this image COPYs nothing); cheapest is this directory:
#     docker build -f docker/netns_audit/Dockerfile -t cv-netns-audit:<tag> docker/netns_audit
#   The repo-root form works identically if that is the habit:
#     docker build -f docker/netns_audit/Dockerfile -t cv-netns-audit:<tag> .
#
# THE TAG IS A CONTRACT WITH THE SCRIPT: `scripts/netns_audit.sh` defaults
# CV_NETNS_AUDIT_IMAGE to `cv-netns-audit:p5c8`; the definition is the line marked
# "Default sidecar tag" — read THAT line, never infer the name from a sibling (G-86). That tag
# is what an armed plane is audited with today, therefore:
#   * build a NEW tag and point the script at it — an overwritten tag has no rollback (G-88):
#       CV_NETNS_AUDIT_IMAGE=cv-netns-audit:<new tag> bash scripts/netns_audit.sh arm <ctr>
#   * when a new tag BECOMES the default, move the default on that line in the same commit,
#     so the script and this file never disagree about which image is meant.
#
# MEASURED AGAINST THE LIVE SIDECAR (2026-08-20, `docker history` + `docker image inspect` +
# in-image `dpkg-query`): the running image is the four base layers below plus one apt layer
# that installed exactly the eight packages named below, `iptables v1.8.11 (nf_tables)`, no
# ENTRYPOINT, no labels, CMD inherited from the base. This file is that image written down —
# not a redesign of it.

# --------------------------------------------------------------------------- #
# BASE — the SAME python:3.11-slim digest that docker/runner/Dockerfile and
# docker/orchestrator/Dockerfile already pin (locked 2026-07-07 at first pull; Python 3.11.15
# on Debian trixie). Deliberately NOT re-resolved from the tag: re-resolving today would pick
# a newer base than the one the live sidecar is made of, and one repo-wide pin is one thing to
# re-pin (CLAUDE §2-7; LOCKED §2 — no floating tags).
# The pin is not a claim: the live sidecar's first four rootfs layers are byte-identical to
# this digest's four (measured 2026-08-20 on both the deployment host and the desktop).
# Python itself is incidental — this image needs a Debian userland with apt, and it rides the
# base the repo already trusts instead of adding a fifth one to keep current.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim@sha256:c20888b6acdd1e63e1c433a185bf3ad162c0288fe484616ce062e0d28add2900

# ── apt VERSION PINS (CLAUDE §2-7; same discipline as docker/selftest_stub/Dockerfile) ──
# The closure is small enough to pin ENTIRELY — `iptables` plus the seven libraries it pulls
# in — so this layer's apt input set is 100 % pinned (the runner's could not be: 247 unpinned
# transitives; this one can, so it is).
# SOURCE OF THE VALUES (measured, never invented — G-64): `dpkg-query -W` inside the LIVE
# sidecar image minus the same query inside the digest-pinned base, 2026-08-20. These are the
# versions the audited plane is actually running, not today's candidates.
# WHEN A PIN DISAPPEARS the build fails loudly (Debian withdraws superseded versions). That is
# the intended §2-7 trade; the re-pin recipe is in the failure message, and the layer prints
# its own manifest so the next person can read the values off the last good image.
RUN set -eu; \
    export DEBIAN_FRONTEND=noninteractive; \
    dpkg-query -W -f='${binary:Package}=${Version}\n' | sort > /tmp/pkgs.before; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        iptables=1.8.11-2 \
        libip4tc2=1.8.11-2 \
        libip6tc2=1.8.11-2 \
        libxtables12=1.8.11-2 \
        libmnl0=1.0.5-3 \
        libnetfilter-conntrack3=1.1.0-1 \
        libnfnetlink0=1.0.2-3 \
        libnftnl11=1.2.9-1 \
    || { echo "ERROR: a pinned version above is no longer served by the Debian archive" \
              "(superseded versions are withdrawn from the index). RE-PIN: read the values" \
              "off the last good sidecar image with 'docker run --rm --entrypoint dpkg-query" \
              "<image> -W', or measure the candidates with 'apt-cache policy iptables';" \
              "update the apt VERSION PINS block in docker/netns_audit/Dockerfile, build a" \
              "NEW tag, and re-run an arm/probe/read round trip before trusting it."; \
         exit 1; }; \
    dpkg-query -W -f='${binary:Package}=${Version}\n' | sort > /tmp/pkgs.after; \
    echo '=== netns audit apt layer manifest (every package this layer installed) ==='; \
    comm -13 /tmp/pkgs.before /tmp/pkgs.after; \
    echo '=== end netns audit apt layer manifest ==='; \
    rm -f /tmp/pkgs.before /tmp/pkgs.after; \
    rm -rf /var/lib/apt/lists/*

# Build-time guard, not decoration (G-26: a silent no-op is the failure mode this whole
# harness exists to prevent). Without it a broken sidecar surfaces three layers away — as
# `arm` failing inside a throwaway container, during a deployment step whose entire claim is
# that nothing failed. The backend is asserted too: the chain semantics `netns_audit.sh`
# depends on were measured on the nf_tables backend.
RUN iptables -V | grep -q 'nf_tables' \
    || { echo "ERROR: iptables is absent, or is not the nf_tables backend this sidecar was" \
              "measured with — refusing to ship an audit image whose counters may not mean" \
              "what scripts/netns_audit.sh reads them as. Got: $(iptables -V 2>&1 || true)"; \
         exit 1; }

# NO ENTRYPOINT — and that is load-bearing, not an omission: `netns_audit.sh` always passes
# the full argv it wants (`iptables …` to read, `sh -c "…"` to arm), and an ENTRYPOINT here
# would silently prepend to it. No CMD either: the base's CMD ["python3"] is inherited (the
# live image has exactly that) and the script never uses it.
#
# NO revision stamp, unlike the three other image-producing paths in this repo (G-66). This
# image carries no cv_infra code — its content is fully determined by the base digest plus the
# pinned versions above — and `scripts/check_plane_skew.sh` does not read this plane. A
# required build arg would only make the one-line build above fail, for provenance this file
# already supplies.
#
# No operator consent value is baked here either (LOCKED §8): this image boots no Isaac and
# takes no operator input — it looks at counters and exits.
