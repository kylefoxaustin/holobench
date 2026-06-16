# Holobench — DISTRIBUTABLE "virtual EVK" image (OSS app + GPL qemu only).
#
# COMPLIANCE: NO NXP BSP artifacts are baked in (no kernel Image, board dtb,
# rootfs/initramfs, M33 SM firmware, or NXP .ko). Those are non-redistributable;
# the operator volume-mounts their own BSP at runtime:
#   docker run -v /my/bsp:/artifacts -e HOLOBENCH_ASSET_ROOT=/artifacts ... holobench:<board>
# with /my/bsp/<board>/{Image,*.dtb,rootfs,m33_image_M2.elf,...}. See docs/DEPLOY.md.
#
# Built from a STAGING context assembled by docker/build.sh, which lays out:
#   ./backend ./frontend ./profiles ./docs ./tools ./vendor  (the Holobench app)
#   ./qemu/qemu-system-aarch64                               (the board's forked QEMU)
# (no ./assets — boot artifacts come from the mounted volume at run time)
#
# Ubuntu 22.04 matches the host the forked QEMU was built on, so apt's qemu
# runtime libraries satisfy the forked binary's shared-lib closure. We install
# stock qemu only to pull those libs + qemu-img (for snapshots); the forked
# binary at /opt/holobench/qemu is what actually runs (via HOLOBENCH_QEMU).
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip \
      qemu-system-arm qemu-utils \
      ca-certificates \
      # Shared libs the forked QEMU is linked against (SDL/GTK/virgl/audio
      # display+audio backends). We run headless, but the binary won't LOAD
      # without them present.
      libsdl2-2.0-0 libgtk-3-0 libgdk-pixbuf-2.0-0 libcairo2 libx11-6 \
      libvirglrenderer1 libpulse0 libsndio7.0 \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/holobench
# Ubuntu 22.04 ships pip 22 which lacks the PEP 660 editable hook; upgrade first.
RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel \
    && pip3 install --no-cache-dir -e /opt/holobench/backend

# Operator mounts their own BSP here (-v /my/bsp:/artifacts); boot artifacts +
# the i.MX95 M33 elf resolve from $HOLOBENCH_ASSET_ROOT/<board>/. Nothing
# NXP-restricted ships in the image.
RUN mkdir -p /artifacts
ENV HOLOBENCH_QEMU=/opt/holobench/qemu/qemu-system-aarch64 \
    HOLOBENCH_ASSET_ROOT=/artifacts \
    PYTHONUNBUFFERED=1

WORKDIR /opt/holobench
EXPOSE 8080
# Runs OPEN (no login) by default. To require auth + show the Admin panel, set
# HOLOBENCH_ADMIN_USER + HOLOBENCH_ADMIN_PASSWORD at run time (seeds an admin on
# startup). Optionally HOLOBENCH_DEMO_LOGIN=user:pass shows a one-click demo box.
CMD ["holobench", "serve", "--host", "0.0.0.0", "--port", "8080"]
