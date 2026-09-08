.PHONY: test run record test-20m image apply

test:
	python3 -m pytest -q

run:
	GGR_DATA_DIR=./data GGR_CONFIG=./config/default.yaml python3 -m uvicorn app.main:app --reload --port 8080

record:
	GGR_DATA_DIR=./data GGR_CONFIG=./config/default.yaml python3 -m recorder.session --once

test-20m:
	GGR_DATA_DIR=./data GGR_CONFIG=./config/default.yaml python3 -m recorder.session --test-20m

image:
	docker build -t ggr-vacations:local .

apply:
	cp config/default.yaml k8s/config.yaml
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -k k8s
