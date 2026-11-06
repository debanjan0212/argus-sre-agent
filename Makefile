.PHONY: install test eval demo demo-pr lint up break reports down

install:            ## install Argus + dev tools
	pip install -e ".[dev]"

test:               ## unit tests
	pytest -q

eval:               ## replay all recorded incidents through MCP and score the agent
	python -m argus eval

demo:               ## one offline investigation, no cluster or API key
	python -m argus investigate --scenario evals/scenarios/03-downstream-saturation.yaml

demo-pr:            ## offline GitOps flow: bad release -> RCA -> rollback commit on a branch
	bash scripts/demo-pr.sh

lint:
	ruff check . && ruff format --check .

up:                 ## kind lab: Prometheus, Loki, Tempo, OTel, demo shop, Argus
	bash scripts/up.sh

break:              ## make break SCENARIO=bad-deploy|oom-kill|saturation|reset
	bash scripts/break.sh $(SCENARIO)

reports:            ## latest Argus reports from the cluster
	kubectl -n argus exec deploy/argus -- python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/reports').read().decode())"

down:
	bash scripts/down.sh
