# SPDX-License-Identifier: GPL-2.0-or-later
"""v3.0 lab (topology) layer: spec validation, loader, and the fabric coordinator's
NIC/MAC/segment wiring — all without booting QEMU (a fake manager records launches)."""

import asyncio

import pytest

from holobench.labs import LabError, list_labs, load_lab
from holobench.labs.coordinator import LabCoordinator, LabState, _mac
from holobench.labs.models import Lab


# --- spec validation -------------------------------------------------------

def test_shipped_labs_load():
    ids = list_labs()
    assert {"eth-pair", "lan-trio", "gateway-lab"} <= set(ids)
    eth = load_lab("eth-pair")
    assert [n.name for n in eth.nodes] == ["nodeA", "nodeB"]
    assert not eth.has_usb_links()
    assert load_lab("gateway-lab").has_usb_links()


def test_eth_link_needs_two_members():
    with pytest.raises(Exception):
        Lab.model_validate({
            "id": "x", "display_name": "x",
            "nodes": [{"name": "a", "profile": "imx91-evk"}],
            "links": [{"type": "eth", "segment": "lan0", "members": ["a"]}],
        })


def test_link_references_unknown_node():
    with pytest.raises(Exception):
        Lab.model_validate({
            "id": "x", "display_name": "x",
            "nodes": [{"name": "a", "profile": "imx91-evk"},
                      {"name": "b", "profile": "imx91-evk"}],
            "links": [{"type": "eth", "segment": "lan0", "members": ["a", "zzz"]}],
        })


def test_duplicate_node_names_rejected():
    with pytest.raises(Exception):
        Lab.model_validate({
            "id": "x", "display_name": "x",
            "nodes": [{"name": "a", "profile": "imx91-evk"},
                      {"name": "a", "profile": "imx91-evk"}],
        })


def test_mac_is_unique_and_qemu_oui():
    macs = {_mac(1, n, i) for n in range(3) for i in range(2)}
    assert len(macs) == 6
    assert all(m.startswith("52:54:00:") for m in macs)


# --- coordinator (fake manager) -------------------------------------------

class _FakeSession:
    def __init__(self, sid, nic_override, usb_override):
        self.id = sid
        self.nic_override = nic_override
        self.usb_override = usb_override
        self.lab_id = None
        self.lab_node = None
        self.quit_called = False

    async def quit(self):
        self.quit_called = True


class _FakeManager:
    """Records launch() calls; never spawns a process."""
    def __init__(self, fail=()):
        self.launches = []
        self.destroyed = []
        self._n = 0
        self._fail = set(fail)  # profile ids that should raise
        self.base_dir = None    # coordinator reads this for usb-socket placement
        self._by_id = {}

    def get(self, sid):
        return self._by_id[sid]

    async def launch(self, profile, *, asset_dir=None, owner=None, minutes=None,
                     nic_override=None, usb_override=None, uart_link_override=None,
                     spi_link_override=None, i2c_link_override=None, can_link_override=None,
                     machine_extra=None, append_extra=None, dtb_override=None):
        if profile.id in self._fail:
            raise RuntimeError(f"boom:{profile.id}")
        self._n += 1
        s = _FakeSession(f"{profile.id}-{self._n}", nic_override, usb_override)
        s.uart_link_override = uart_link_override
        s.spi_link_override = spi_link_override
        s.i2c_link_override = i2c_link_override
        s.can_link_override = can_link_override
        s.machine_extra = machine_extra
        s.append_extra = append_extra
        s.dtb_override = dtb_override
        self.launches.append(s)
        self._by_id[s.id] = s
        return s

    async def destroy(self, sid):
        self.destroyed.append(sid)


def test_eth_lab_wires_one_mcast_nic_per_member():
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(load_lab("eth-pair")))
    assert running.state == LabState.RUNNING
    assert len(mgr.launches) == 2
    # Both members get exactly one socket,mcast NIC on the SAME group, unique MACs.
    nics = [s.nic_override for s in mgr.launches]
    assert all(len(n) == 1 for n in nics)
    groups = {n[0].split("mcast=")[1].split(",")[0] for n in nics}
    assert len(groups) == 1                      # same segment -> same group
    macs = {n[0].split("mac=")[1] for n in nics}
    assert len(macs) == 2                        # unique per node (no collision)
    # Sessions tagged with their lab identity.
    assert all(s.lab_id == "eth-pair" and s.lab_node for s in mgr.launches)


def test_auto_ip_assigns_static_addresses_and_fabric_dtb():
    # auto_ip (default) hands each eth-segment member a static IP via the kernel
    # `ip=` cmdline (append_extra), and an i.MX95 node boots its net.fabric_dtb so
    # ENETC actually enumerates eth0. Both distinct addresses on one /24.
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(load_lab("lan-trio")))   # 2x imx95 + 1x imx93
    by_node = {s.lab_node: s for s in mgr.launches}
    ips = {}
    for name, s in by_node.items():
        assert s.append_extra and s.append_extra.startswith("ip=")
        assert ":eth0:off" in s.append_extra
        ips[name] = s.append_extra.split("ip=")[1].split(":")[0]
    assert len(set(ips.values())) == 3                          # distinct addresses
    assert all(ip.startswith("10.") for ip in ips.values())    # same /24 family
    # the i.MX95 nodes boot the ENETC fixed-link dtb; the i.MX93 (FEC binds) doesn't
    assert by_node["a95"].dtb_override == "imx95-19x19-evk-enetc.dtb"
    assert by_node["b95"].dtb_override == "imx95-19x19-evk-enetc.dtb"
    assert by_node["c93"].dtb_override is None
    # node_ips is exposed for the CLI/UI
    assert set(running.node_ips) == {"a95", "b95", "c93"}


def test_auto_ip_off_leaves_a_bare_wire():
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(load_lab("eth-pair"), auto_ip=False))
    assert all(s.append_extra is None for s in mgr.launches)
    assert running.node_ips == {}


def test_separate_segments_get_isolated_groups():
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    lab = Lab.model_validate({
        "id": "two-seg", "display_name": "two segments",
        "nodes": [{"name": "a", "profile": "imx91-evk"},
                  {"name": "b", "profile": "imx91-evk"},
                  {"name": "c", "profile": "imx91-evk"}],
        "links": [
            {"type": "eth", "segment": "lanA", "members": ["a", "b"]},
            {"type": "eth", "segment": "lanB", "members": ["b", "c"]},
        ],
    })
    asyncio.run(coord.launch(lab))
    by_node = {s.id.rsplit("-", 1)[0] + ":" + str(i): s
               for i, s in enumerate(mgr.launches)}  # noqa
    # node b is on both segments -> two NICs, two different groups.
    b = [s for s in mgr.launches if s.lab_node == "b"][0]
    assert len(b.nic_override) == 2
    grp = lambda spec: spec.split("mcast=")[1].split(",")[0]
    assert grp(b.nic_override[0]) != grp(b.nic_override[1])


def test_mixed_lab_sets_per_node_nic_model():
    # mcx93-eth: the MCX node gets model=mcxn-enet, the 93 gets model=imx.enet
    # (from each profile's net.fabric_nic_model), on one shared mcast group.
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(load_lab("mcx93-eth")))
    assert running.state == LabState.RUNNING
    by_node = {s.lab_node: s for s in mgr.launches}
    mcx = by_node["mcx"].nic_override[0]
    gw = by_node["gw"].nic_override[0]
    assert "model=mcxn-enet" in mcx, mcx
    assert "model=imx.enet" in gw, gw
    # same segment -> same group; unique MACs.
    grp = lambda s: s.split("mcast=")[1].split(",")[0]
    assert grp(mcx) == grp(gw)
    assert mcx.split("mac=")[1].split(",")[0] != gw.split("mac=")[1].split(",")[0]


def test_usb_lab_wires_host_and_device():
    # The gateway-lab launches (USB validated end-to-end -> no env gate) and each
    # end gets its usbredir role from the profile: the i.MX93 host gets `-device
    # usb-redir` (client/importer), the MCXN947 device gets a listening exporter
    # chardev, both bound to the SAME per-link unix socket the coordinator owns.
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(load_lab("gateway-lab")))
    assert running.state == LabState.RUNNING
    by_node = {s.lab_node: s for s in mgr.launches}

    gw = by_node["gw"].usb_override        # i.MX93 host
    sensor = by_node["sensor"].usb_override  # MCXN947 device
    # Host = stock usb-redir client over a reconnecting socket chardev.
    assert "-chardev" in gw and "-device" in gw
    assert any("usb-redir" in a for a in gw)
    assert any("reconnect-ms=2000" in a for a in gw)
    # Device = the exporter: JUST a listening socket chardev (the model auto-binds
    # its HS usbredir core to the well-known id) — no -device, no -global.
    assert "-chardev" in sensor and "-global" not in sensor and "-device" not in sensor
    assert any("server=on" in a for a in sensor)
    # Both ends point at the SAME socket path (the link's shared unix socket).
    host_sock = gw[gw.index("-chardev") + 1].split("path=")[1].split(",")[0]
    dev_sock = sensor[sensor.index("-chardev") + 1].split("path=")[1].split(",")[0]
    assert host_sock == dev_sock
    assert running.usb_socks == [host_sock]
    # Host id is coordinator-allocated; device id is the well-known HS-core binding.
    assert "id=hbusb0" in gw[gw.index("-chardev") + 1]
    assert "id=mcxn-usbhs" in sensor[sensor.index("-chardev") + 1]


def test_uart_lab_wires_symmetric_socket_bridge():
    # The uart-link-91 lab bridges two i.MX91 over LPUART2: one end is the socket
    # server, the other the client, both on the SAME socket + chardev id, and both
    # boot the LPUART2-enabled dtb. The link UART is an extra -serial (serial_hd(1)).
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(load_lab("uart-link-91")))
    assert running.state == LabState.RUNNING
    by_node = {s.lab_node: s for s in mgr.launches}
    a = by_node["boardA"].uart_link_override
    b = by_node["boardB"].uart_link_override
    assert "-chardev" in a and "-serial" in a and "chardev:hbuart0" in a
    assert any("server=on" in x for x in a)     # boardA listens
    assert any(("server=off" in x) for x in b)  # boardB connects
    # same socket path both ends
    a_sock = a[a.index("-chardev") + 1].split("path=")[1].split(",")[0]
    b_sock = b[b.index("-chardev") + 1].split("path=")[1].split(",")[0]
    assert a_sock == b_sock
    assert running.usb_socks == [a_sock]
    # both boot the LPUART2-enabled dtb
    assert by_node["boardA"].dtb_override == "imx91-11x11-evk-uartlink.dtb"
    assert by_node["boardB"].dtb_override == "imx91-11x11-evk-uartlink.dtb"


def test_spi_lab_wires_symmetric_socket_bridge():
    # The spi-link-91 lab bridges two i.MX91 over LPSPI1: one end socket server,
    # the other a reconnecting client, both with `-device spi-link,bus=lpspi1` and
    # booting the LPSPI1+spidev dtb.
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(load_lab("spi-link-91")))
    assert running.state == LabState.RUNNING
    by_node = {s.lab_node: s for s in mgr.launches}
    a = by_node["boardA"].spi_link_override
    b = by_node["boardB"].spi_link_override
    assert any("spi-link,bus=lpspi1" in x for x in a)
    assert any("spi-link,bus=lpspi1" in x for x in b)
    assert any("server=on" in x for x in a)                     # boardA listens
    assert any("server=off" in x and "reconnect-ms" in x for x in b)  # boardB reconnecting client
    a_sock = a[a.index("-chardev") + 1].split("path=")[1].split(",")[0]
    b_sock = b[b.index("-chardev") + 1].split("path=")[1].split(",")[0]
    assert a_sock == b_sock
    assert by_node["boardA"].dtb_override == "imx91-11x11-evk-spilink.dtb"
    assert by_node["boardB"].dtb_override == "imx91-11x11-evk-spilink.dtb"


def test_mixed_can_lab_wires_cross_arch():
    # can-link-91-mcx: a bare-metal MCXN947 (arm) + a Linux i.MX91 (aarch64) on one
    # can-host-chardev socket, each with its OWN machine props (the cross-arch tell:
    # mcx=canbus0=cb, imx91=canbus0=cb,canbus1=cb). mcx=server, imx91=reconnecting client.
    mgr = _FakeManager()
    running = asyncio.run(LabCoordinator(mgr).launch(load_lab("can-link-91-mcx")))
    assert running.state == LabState.RUNNING
    by = {s.lab_node: s for s in mgr.launches}
    assert any("can-host-chardev" in x for x in by["mcx"].can_link_override)
    assert any("server=on" in x for x in by["mcx"].can_link_override)
    assert any("server=off" in x and "reconnect-ms" in x for x in by["board91"].can_link_override)
    assert by["mcx"].machine_extra == "canbus0=cb"                      # MCU: one can-bus
    assert by["board91"].machine_extra == "canbus0=cb,canbus1=cb"       # i.MX: two
    # same socket path bridges the two SoCs
    sock = lambda ov: ov[ov.index("-chardev") + 1].split("path=")[1].split(",")[0]
    assert sock(by["mcx"].can_link_override) == sock(by["board91"].can_link_override)


def test_mixed_uart_lab_wires_cross_arch():
    # uart-link-imx-mcx: MCX (bare-metal, no dtb) <-> imx91 (Linux, LPUART2 dtb) on
    # one chardev socket. The imx91 needs its uartlink dtb override; the MCX doesn't.
    mgr = _FakeManager()
    running = asyncio.run(LabCoordinator(mgr).launch(load_lab("uart-link-imx-mcx")))
    assert running.state == LabState.RUNNING
    by = {s.lab_node: s for s in mgr.launches}
    assert any("chardev:hbuart0" in x for x in by["mcx"].uart_link_override)
    assert any("server=on" in x for x in by["mcx"].uart_link_override)
    assert any("server=off" in x for x in by["board91"].uart_link_override)
    assert by["mcx"].dtb_override is None                               # bare-metal, no dtb
    assert by["board91"].dtb_override == "imx91-11x11-evk-uartlink.dtb" # Linux needs LPUART2 enabled


def test_i2c_lab_wires_symmetric_socket_bridge():
    # i2c-link-91: two i.MX91 over LPI2C3, one server / one reconnecting client,
    # both with -device i2c-link,bus=lpi2c3. No dtb (stock EVK dtb enables LPI2C3).
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(load_lab("i2c-link-91")))
    assert running.state == LabState.RUNNING
    by_node = {s.lab_node: s for s in mgr.launches}
    a = by_node["boardA"].i2c_link_override
    b = by_node["boardB"].i2c_link_override
    assert any("i2c-link,bus=lpi2c3" in x for x in a)
    assert any("i2c-link,bus=lpi2c3" in x for x in b)
    assert any("server=on" in x for x in a)
    assert any("server=off" in x and "reconnect-ms" in x for x in b)
    a_sock = a[a.index("-chardev") + 1].split("path=")[1].split(",")[0]
    b_sock = b[b.index("-chardev") + 1].split("path=")[1].split(",")[0]
    assert a_sock == b_sock
    assert by_node["boardA"].dtb_override is None   # no dtb patch needed for i2c


def test_can_lab_wires_symmetric_socket_bridge():
    # The can-link-91 lab bridges two i.MX91 over FlexCAN: one end socket server,
    # the other reconnecting client, both with -object can-bus + -object
    # can-host-chardev, and both get the canbus machine props (machine_extra).
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(load_lab("can-link-91")))
    assert running.state == LabState.RUNNING
    by_node = {s.lab_node: s for s in mgr.launches}
    a = by_node["boardA"].can_link_override
    b = by_node["boardB"].can_link_override
    assert any("can-bus,id=cb" in x for x in a)
    assert any("can-host-chardev" in x for x in a)
    assert any("server=on" in x for x in a)                          # boardA listens
    assert any("server=off" in x and "reconnect-ms" in x for x in b)  # boardB client
    a_sock = a[a.index("-chardev") + 1].split("path=")[1].split(",")[0]
    b_sock = b[b.index("-chardev") + 1].split("path=")[1].split(",")[0]
    assert a_sock == b_sock
    # both get the canbus machine props (wired onto -machine in build_command)
    assert by_node["boardA"].machine_extra == "canbus0=cb,canbus1=cb"
    assert by_node["boardB"].machine_extra == "canbus0=cb,canbus1=cb"


def test_usb_lab_errors_when_a_profile_lacks_a_role():
    # The device node's profile has no usb.device role -> that node is flagged in
    # node_errors (honest fault), not silently mis-wired.
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    lab = Lab.model_validate({
        "id": "usb-noroles", "display_name": "usb no roles",
        "nodes": [{"name": "h", "profile": "imx93-evk-sd"},
                  {"name": "d", "profile": "imx91-evk"}],  # imx91 has no usb.device
        "links": [{"type": "usb", "host": "h", "device": "d"}],
    })
    running = asyncio.run(coord.launch(lab))
    assert "d" in running.node_errors
    assert "usb.device" in running.node_errors["d"]


def test_partial_lab_when_one_node_fails():
    mgr = _FakeManager(fail={"imx91-evk"})  # nodeB profile also imx91-evk... both fail
    coord = LabCoordinator(mgr)
    # Use a mixed lab so one succeeds, one fails.
    lab = Lab.model_validate({
        "id": "mixed", "display_name": "mixed",
        "nodes": [{"name": "ok", "profile": "imx93-evk"},
                  {"name": "bad", "profile": "imx91-evk"}],
        "links": [{"type": "eth", "segment": "lan0", "members": ["ok", "bad"]}],
    })
    running = asyncio.run(coord.launch(lab))
    assert running.state == LabState.PARTIAL
    assert "bad" in running.node_errors
    assert running.node_sessions.get("ok")


def _fake_request(labs):
    import types
    from holobench.auth import User
    return types.SimpleNamespace(state=types.SimpleNamespace(user=User("tester", "admin")),
                                 app=types.SimpleNamespace(state=types.SimpleNamespace(
                                     labs=labs, auth=types.SimpleNamespace(enabled=False))))


def test_api_catalog_and_gated_launch():
    import importlib
    A = importlib.import_module("holobench.api.app")
    coord = LabCoordinator(_FakeManager())
    A.app_ref.state.labs = coord            # inject test coordinator
    req = _fake_request(coord)

    out = A.get_labs(req)
    ids = {e["id"]: e for e in out["catalog"]}
    assert "eth-pair" in ids and ids["eth-pair"]["launchable"] is True
    # USB labs are launchable now that the link is validated end-to-end.
    assert ids["gateway-lab"]["launchable"] is True
    assert ids["gateway-lab"]["gated_reason"] is None
    assert ids["gateway-lab"]["usb_links"] == 1

    # Unknown lab -> 404.
    with pytest.raises(A.HTTPException) as ei:
        A.get_lab("does-not-exist", req)
    assert ei.value.status_code == 404


def test_double_launch_rejected_and_stop_frees_group():
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    asyncio.run(coord.launch(load_lab("eth-pair")))
    with pytest.raises(LabError, match="already running"):
        asyncio.run(coord.launch(load_lab("eth-pair")))
    asyncio.run(coord.stop("eth-pair"))
    assert len(mgr.destroyed) == 2
    assert coord.peek("eth-pair") is None
    # group was freed -> relaunch works and reuses the lowest group octet.
    asyncio.run(coord.launch(load_lab("eth-pair")))
    assert coord.peek("eth-pair") is not None


# --- the SCHEDULE: labs that test TIME, not just topology -------------------
#
# The fleet's three emulator sessions (rt1180 + mcxn947 + 95) found a QEMU
# can_receive()/qemu_flush_queued_packets() queue stall that is structurally invisible
# to any 2-node test AND to any 3-node test whose nodes boot together: "THE BUG CLASS
# LIVES IN TIME, NOT TOPOLOGY." These guard the machinery that lets a lab express it.

def _sched_lab(**over):
    spec = {
        "id": "sched", "display_name": "sched",
        "nodes": [
            {"name": "a", "profile": "imx91-evk", "start_at": 0},
            {"name": "b", "profile": "imx91-evk", "start_at": 0.05, "stop_at": 0.12},
            {"name": "c", "profile": "imx91-evk", "start_at": 0.10},
        ],
        "links": [{"type": "eth", "segment": "s0", "members": ["a", "b", "c"]}],
    }
    spec.update(over)
    return Lab.model_validate(spec)


def test_schedule_staggers_arrivals_in_start_order():
    lab = _sched_lab()
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    running = asyncio.run(coord.launch(lab))
    assert running.state == LabState.RUNNING
    # Arrivals happen in start_at order, and each one is TIMED (measured, not assumed).
    assert [s.lab_node for s in mgr.launches] == ["a", "b", "c"]
    assert running.node_arrivals["a"] < running.node_arrivals["b"] < running.node_arrivals["c"]
    # 'c' really did join a segment that was already live — it arrived after 'a'.
    assert running.node_arrivals["c"] >= 0.10


def test_scheduled_departure_fires_and_is_recorded_as_a_departure():
    # The whole point: a node that is gone because WE retired it must never be
    # confused with a node that is gone because it died. The coordinator records it.
    lab = _sched_lab()
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)

    async def go():
        running = await coord.launch(lab)
        await asyncio.sleep(0.25)          # past b's stop_at
        return running

    running = asyncio.run(go())
    assert running.departed("b")
    assert not running.departed("a") and not running.departed("c")
    assert running.node_departures["b"] >= 0.12


def test_departure_quits_the_node_but_does_NOT_destroy_its_session():
    # REGRESSION. destroy() calls cleanup(), which rmtree's the session work dir —
    # and that dir holds the node's CONSOLE LOG. A departure that destroyed the
    # session would erase the evidence of the very node whose departure is the point
    # ("did it PASS before it left?"). Departure must quit() and leave the log.
    lab = _sched_lab()
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)

    async def go():
        running = await coord.launch(lab)
        await asyncio.sleep(0.25)
        return running

    running = asyncio.run(go())
    b_sid = running.node_sessions["b"]
    assert mgr.get(b_sid).quit_called, "departed node must be powered off"
    assert b_sid not in mgr.destroyed, "departure must NOT destroy the session (rmtree's the console log)"


def test_a_node_cannot_leave_before_it_arrives():
    with pytest.raises(Exception):
        Lab.model_validate({
            "id": "x", "display_name": "x",
            "nodes": [{"name": "a", "profile": "imx91-evk",
                       "start_at": 10, "stop_at": 5}],
        })


def test_lab_can_pin_a_node_mac():
    lab = _sched_lab(nodes=[
        {"name": "a", "profile": "imx91-evk", "mac": "02:4d:43:58:00:01"},
        {"name": "b", "profile": "imx91-evk"},
    ], links=[{"type": "eth", "segment": "s0", "members": ["a", "b"]}])
    mgr = _FakeManager()
    coord = LabCoordinator(mgr)
    asyncio.run(coord.launch(lab))
    by_node = {s.lab_node: s.nic_override[0] for s in mgr.launches}
    assert "mac=02:4d:43:58:00:01" in by_node["a"]        # pinned
    assert "mac=52:54:00:" in by_node["b"]                # auto, unchanged


def test_existing_labs_are_unstaggered_by_default():
    # Defaults must preserve the old behaviour exactly: everyone at t=0, nobody leaves.
    for lid in ("eth-pair", "lan-trio", "gateway-lab", "uart-link-91"):
        lab = load_lab(lid)
        assert not lab.is_staggered, f"{lid} unexpectedly staggered"
        assert lab.horizon_s == 0


def test_the_three_node_l2_lab_is_staggered_and_someone_leaves_early():
    # The lab the fleet asked for, exactly as they specified it: staggered arrivals,
    # a departure mid-run, and raw L2 (no auto-IP — these nodes talk in ethertypes).
    lab = load_lab("mcx-rt1180-95-l2")
    assert lab.is_staggered
    assert lab.auto_ip is False
    byname = {n.name: n for n in lab.nodes}
    # imx95 (the slow Linux node) goes first and broadcasts alone into an empty segment.
    assert byname["imx95"].start_at == 0
    # the others join a live segment, at different times
    assert byname["mcx"].start_at > 0
    assert byname["rt1180"].start_at > byname["mcx"].start_at
    # and exactly one node leaves early, while the other two keep running
    leavers = [n.name for n in lab.nodes if n.stop_at is not None]
    assert leavers == ["mcx"]
    assert byname["mcx"].stop_at > byname["rt1180"].start_at
    # three distinct SoCs on ONE segment
    seg = [l for l in lab.links if l.type == "eth"][0]
    assert sorted(seg.members) == ["imx95", "mcx", "rt1180"]
