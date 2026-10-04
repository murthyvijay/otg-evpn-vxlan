#!/usr/bin/env python3
"""Phase 0 feasibility probe.

Answers, with evidence, whether this host + KENG/Ixia-c edition can run the lab:
controller reachable, edition/licence accepts N ports, protocol engine resolves
ARP against the FRR gateway, traffic passes loss-free, latency metrics exist,
and CPU headroom while 8 engines run. Writes results/phase0/<ts>/report.{json,md}.

Exit codes: 0 GO, 1 NO-GO (a required check failed), 2 probe could not run.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import snappi

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


class Probe:
    def __init__(self, args):
        self.args = args
        self.n = args.ports
        self.checks = []  # (name, status, detail, required)
        self.api = None

    def record(self, name, status, detail, required=True):
        self.checks.append({"check": name, "status": status, "detail": str(detail), "required": required})
        print(f"[{status}] {name}: {detail}", flush=True)
        return status == PASS

    # ---- helpers -------------------------------------------------------
    def wait(self, what, fn, timeout, interval=2):
        """Poll fn() until it returns truthy or timeout; last exception is reported."""
        deadline, last = time.time() + timeout, None
        while time.time() < deadline:
            try:
                res = fn()
                if res:
                    return res, None
            except Exception as e:  # transient while engines come up
                last = e
            time.sleep(interval)
        return None, last or f"{what}: timeout after {timeout}s"

    def set_state(self, kind, start):
        cs = self.api.control_state()
        if kind == "protocol":
            cs.choice = cs.PROTOCOL
            cs.protocol.choice = cs.protocol.ALL
            cs.protocol.all.state = cs.protocol.all.START if start else cs.protocol.all.STOP
        else:
            cs.choice = cs.TRAFFIC
            cs.traffic.choice = cs.traffic.FLOW_TRANSMIT
            ft = cs.traffic.flow_transmit
            ft.state = ft.START if start else ft.STOP
        self.api.set_control_state(cs)

    def build_config(self):
        cfg = self.api.config()
        half = self.n // 2
        for i in range(1, self.n + 1):
            p = cfg.ports.add(name=f"p{i}", location=f"localhost:{5554 + i}+localhost:{50070 + i}")
            d = cfg.devices.add(name=f"d{i}")
            eth = d.ethernets.add(name=f"d{i}.eth", mac=f"02:00:00:00:{i:02x}:02")
            eth.connection.port_name = p.name
            eth.ipv4_addresses.add(name=f"d{i}.ip", address=f"10.0.{i}.2", gateway=f"10.0.{i}.1", prefix=24)
        # licence capacity is the sum of configured port speeds: pin 1GE per port
        l1 = cfg.layer1.add(name="l1", port_names=[f"p{i}" for i in range(1, self.n + 1)])
        l1.speed = l1.SPEED_1_GBPS
        # pairs i <-> i+half, both directions (mirrors Option A tenant pairing)
        pkts = self.args.pps * self.args.seconds
        for a in range(1, half + 1):
            for s, d in ((a, a + half), (a + half, a)):
                f = cfg.flows.add(name=f"p{s}>p{d}")
                f.tx_rx.device.tx_names = [f"d{s}.ip"]
                f.tx_rx.device.rx_names = [f"d{d}.ip"]
                f.packet.ethernet().ipv4()
                f.size.fixed = self.args.size
                f.rate.pps = self.args.pps
                f.duration.fixed_packets.packets = pkts
                f.metrics.enable = True
                f.metrics.loss = True
                f.metrics.latency.enable = True
                f.metrics.latency.mode = f.metrics.latency.CUT_THROUGH
        return cfg

    def flow_metrics(self):
        req = self.api.metrics_request()
        req.flow.flow_names = []
        return self.api.get_metrics(req).flow_metrics

    def cpu_sample(self):
        load1 = os.getloadavg()[0]
        try:
            out = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}"],
                capture_output=True, text=True, timeout=30, check=True).stdout
            per = {l.split()[0]: float(l.split()[1].rstrip("%")) for l in out.splitlines()
                   if l.startswith("clab-evpn-p0-")}
        except Exception as e:
            per = {"error": str(e)}
        return load1, per

    # ---- checks ----------------------------------------------------------
    def run(self):
        a = self.args
        self.api = snappi.api(location=a.api, verify=False)
        ver, err = self.wait("controller", lambda: self.api.get_version(), a.timeout)
        if not self.record("controller_api", PASS if ver else FAIL,
                           f"app {ver.app_version}, api {ver.api_spec_version}" if ver else err):
            return 2

        edition = "KENG (LICENSE_SERVERS set)" if os.environ.get("LICENSE_SERVERS") else "Community Edition"
        try:
            self.api.set_config(self.build_config())
            self.record("licence_ports", PASS, f"{edition}: {self.n} x 1GE accepted")
        except Exception as e:
            self.record("licence_ports", FAIL, f"{edition}: set_config rejected {self.n} ports: {e}")
            return 1

        try:
            self.set_state("protocol", True)
            names = [f"d{i}.eth" for i in range(1, self.n + 1)]

            def resolved():
                req = self.api.states_request()
                req.ipv4_neighbors.ethernet_names = names
                ok = [s for s in self.api.get_states(req).ipv4_neighbors if s.link_layer_address]
                return ok if len(ok) >= self.n else None
            res, err = self.wait("arp", resolved, a.timeout)
            if not self.record("arp_gateway", PASS if res else FAIL,
                               f"{self.n}/{self.n} gateways resolved" if res else err):
                return 1

            self.set_state("traffic", True)
            time.sleep(min(a.seconds / 2, 5))
            load1, per = self.cpu_sample()
            done, err = self.wait("traffic", lambda: all(m.transmit == "stopped" for m in self.flow_metrics()),
                                  a.seconds + a.timeout)
            fm = self.flow_metrics()
        finally:
            for kind in ("traffic", "protocol"):  # always leave engines idle
                try:
                    self.set_state(kind, False)
                except Exception as e:
                    print(f"cleanup: stop {kind} failed: {e}", file=sys.stderr)

        if not done:
            self.record("traffic_completes", FAIL, err)
        tx = sum(m.frames_tx for m in fm)
        rx = sum(m.frames_rx for m in fm)
        if tx == 0:
            self.record("traffic_loss", FAIL, "no frames transmitted - measurement invalid")
        else:
            lossy = {m.name: m.frames_tx - m.frames_rx for m in fm if m.frames_tx != m.frames_rx}
            self.record("traffic_loss", PASS if not lossy else FAIL,
                        f"tx={tx} rx={rx}" + (f" per-flow deficit {lossy}" if lossy else ""))
        lat = [m.latency.average_ns for m in fm if m.frames_rx and m.latency]
        self.record("latency_metrics", PASS if lat and all(v > 0 for v in lat) else FAIL,
                    f"avg ns per flow {[round(v) for v in lat]}" if lat else "no latency values returned")
        nproc = os.cpu_count() or 1
        headroom = load1 < 0.8 * nproc
        self.record("cpu_headroom", PASS if headroom else WARN,
                    f"load1={load1:.2f} on {nproc} vCPU; per-container %: {per}", required=False)
        self.metrics = [{"flow": m.name, "tx": m.frames_tx, "rx": m.frames_rx,
                         "lat_avg_ns": m.latency.average_ns if m.latency else None} for m in fm]
        return 0 if all(c["status"] != FAIL for c in self.checks if c["required"]) else 1

    def report(self, rc):
        out = os.path.join(self.args.out, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        os.makedirs(out, exist_ok=True)
        verdict = {0: "GO", 1: "NO-GO", 2: "PROBE ERROR"}[rc]
        data = {"verdict": verdict, "ports": self.n, "args": vars(self.args),
                "checks": self.checks, "flows": getattr(self, "metrics", [])}
        with open(os.path.join(out, "report.json"), "w") as f:
            json.dump(data, f, indent=2)
        rows = "\n".join(f"| {c['check']} | {c['status']} | {c['detail']} |" for c in self.checks)
        with open(os.path.join(out, "report.md"), "w") as f:
            f.write(f"# Phase 0 probe: {verdict}\n\nPorts: {self.n}\n\n"
                    f"| Check | Status | Detail |\n|---|---|---|\n{rows}\n")
        print(f"\nVerdict: {verdict}  ->  {out}/report.md")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api", default=os.environ.get("OTG_API", "https://localhost:8443"))
    ap.add_argument("--ports", type=int, default=8, choices=(2, 4, 6, 8))
    ap.add_argument("--pps", type=int, default=1000)
    ap.add_argument("--seconds", type=int, default=10)
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--timeout", type=int, default=120, help="per-wait timeout (s)")
    ap.add_argument("--out", default="results/phase0")
    args = ap.parse_args()
    p = Probe(args)
    try:
        rc = p.run()
    except Exception as e:  # unexpected: still emit a report
        p.record("unexpected_error", FAIL, repr(e))
        rc = 2
    p.report(rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
