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


class _FakeManager:
    """Records launch() calls; never spawns a process."""
    def __init__(self, fail=()):
        self.launches = []
        self.destroyed = []
        self._n = 0
        self._fail = set(fail)  # profile ids that should raise
        self.base_dir = None    # coordinator reads this for usb-socket placement

    async def launch(self, profile, *, asset_dir=None, owner=None, minutes=None,
                     nic_override=None, usb_override=None):
        if profile.id in self._fail:
            raise RuntimeError(f"boom:{profile.id}")
        self._n += 1
        s = _FakeSession(f"{profile.id}-{self._n}", nic_override, usb_override)
        self.launches.append(s)
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
