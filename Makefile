SHELL := /bin/bash
include versions.env
PORTS ?= 8
LICENSE_SERVERS ?=
TOPO := phase0/topo.clab.yml
VENV := .venv
NODES := ixc,gw,$(shell for i in $$(seq 1 $(PORTS)); do printf 'te%s,pe%s,' $$i $$i; done | sed 's/,$$//')
CLAB_ENV := KENG_CONTROLLER=$(KENG_CONTROLLER) IXIA_C_TE=$(IXIA_C_TE) IXIA_C_PE=$(IXIA_C_PE) \
            FRR_IMAGE=$(FRR_IMAGE) LICENSE_SERVERS=$(LICENSE_SERVERS)

.PHONY: all install preflight deploy probe clean
all: install preflight deploy probe

install: $(VENV)/.ok
	@command -v containerlab >/dev/null && containerlab version | grep -q "$(CLAB_VERSION)" || \
	  bash -c "$$(curl -sL https://get.containerlab.dev)" -- -v $(CLAB_VERSION)
$(VENV)/.ok: requirements.txt
	python3 -m venv $(VENV) && $(VENV)/bin/pip install -q -r requirements.txt && touch $@

# Fail fast on missing tools, kernel modules or unpullable image tags.
preflight:
	@for c in docker containerlab python3; do command -v $$c >/dev/null || { echo "missing: $$c"; exit 1; }; done
	@docker info >/dev/null 2>&1 || { echo "docker daemon not reachable (is $$USER in the docker group?)"; exit 1; }
	@sudo modprobe vxlan || { echo "vxlan kernel module unavailable"; exit 1; }
	@case "$(PORTS)" in 2|4|6|8) ;; *) echo "PORTS must be 2, 4, 6 or 8"; exit 1;; esac
	@[ -n "$(LICENSE_SERVERS)" ] || [ $(PORTS) -le 4 ] || { echo "PORTS=$(PORTS) needs KENG: set LICENSE_SERVERS (CE allows 4)"; exit 1; }
	@for i in $(KENG_CONTROLLER) $(IXIA_C_TE) $(IXIA_C_PE) $(FRR_IMAGE); do \
	  docker pull -q $$i >/dev/null || { echo "cannot pull $$i"; exit 1; }; done
	@echo "preflight OK (PORTS=$(PORTS), edition=$$([ -n "$(LICENSE_SERVERS)" ] && echo KENG || echo CE))"

deploy:
	sudo $(CLAB_ENV) containerlab deploy -t $(TOPO) --reconfigure --node-filter $(NODES)

probe:
	$(VENV)/bin/python phase0/probe.py --ports $(PORTS)

clean:
	-sudo $(CLAB_ENV) containerlab destroy -t $(TOPO) --cleanup
