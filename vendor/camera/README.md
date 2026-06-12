# Vendored ISI virtual-camera capture helpers

These are the in-guest V4L2 capture clients for the **virtual camera** feature
(see `ROADMAP.md`). Holobench stages the per-board binary into a session's
virtio-9p share so the guest runs it from `/mnt`.

## Why bundled (not a shipped tool)

The i.MX8-ISI capture media links start **disabled**, and the boards' shipped
`imx-image-core` rootfs has **no `media-ctl`, no `v4l2-ctl`, and no gst
`v4l2src`** — so nothing in the rootfs can set up the graph and capture. On the
i.MX95 specifically, even where `media-ctl` exists (imx-image-full) it can't
satisfy the 8-channel crossbar's per-stream `link_validate` (STREAMON EPIPEs).
Each of these clients does the `MEDIA_IOC_SETUP_LINK` + per-stream
`SUBDEV_S_FMT` + `REQBUFS/STREAMON/DQBUF` itself, in C. Validated byte-exact by
the respective emulator session.

## Provenance & license

`SPDX-License-Identifier: GPL-2.0-or-later`, © Kyle Fox. Vendored verbatim from
the public emulator repos (the canonical upstream source):

| File | Upstream |
|---|---|
| `imx95_v4l2_cap.c` | `github.com/kylefoxaustin/95emulator` → `tests/camera/v4l2_cap.c` |
| `imx91_v4l2_cap.c` | `github.com/kylefoxaustin/91emulator` → `tests/camera-imx91/v4l2_cap.c` |
| `imx93_v4l2_cap.c` | `github.com/kylefoxaustin/qemu-imx93` → `tests/camera-imx93/v4l2_cap.c` |

These are **standalone command-line tools** shipped *alongside* Holobench, not
linked into it — so Holobench's own Apache-2.0 license is unaffected (standard
"GPL tool in the image" aggregation). The C sources here satisfy GPL source
availability for the prebuilt `bin/` binaries.

## Build

Prebuilt static aarch64 binaries live in `bin/` (committed; zero shared-lib deps,
run on any aarch64 rootfs). To rebuild:

    tools/build-capture-helpers.sh        # needs gcc-aarch64-linux-gnu

## Run (in the guest, from /mnt)

    /mnt/imxNN-isi-capture cap /dev/video0     # setup links + capture -> ./frame*.raw
    /mnt/imxNN-isi-capture topo /dev/media0    # dump the media graph
