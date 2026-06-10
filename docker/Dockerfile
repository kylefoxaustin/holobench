# Holobench — fat "virtual EVK" image.
#
# Built from a STAGING context assembled by tools/docker-build.sh, which lays out:
#   ./backend ./frontend ./profiles ./docs ./tools   (the Holobench app)
#   ./qemu/qemu-system-aarch64                        (the board's forked QEMU)
#   ./assets/<board>/{Image,*.dtb,initrd.cpio.gz}     (baked boot artifacts)
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

ENV HOLOBENCH_QEMU=/opt/holobench/qemu/qemu-system-aarch64 \
    HOLOBENCH_ASSET_ROOT=/opt/holobench/assets \
    PYTHONUNBUFFERED=1

WORKDIR /opt/holobench
EXPOSE 8080
# Set HOLOBENCH_TOKEN at run time to require auth on /api/*.
CMD ["holobench", "serve", "--host", "0.0.0.0", "--port", "8080"]
