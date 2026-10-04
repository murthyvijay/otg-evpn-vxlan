# otg-evpn-vxlan
Open Traffic Generator based multi-tenant EVPN VXLAN Lab

## Phase 0: feasibility probe

Deploys 8 Ixia-c ports (traffic + protocol engine) around one FRR gateway and
reports GO / NO-GO for: controller API, licence capacity, ARP to gateway,
loss-free traffic, latency metrics, CPU headroom. Versions are pinned in
`versions.env`.

```bash
make install                                   # containerlab + python venv
make preflight deploy probe LICENSE_SERVERS=<license-server-ip>   # KENG, 8 ports
make preflight deploy probe PORTS=4            # Community Edition, 4 ports
make clean
```

The report lands in `results/phase0/<timestamp>/report.md`.
Exit codes: 0 GO, 1 NO-GO, 2 probe could not run.
