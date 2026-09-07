.PHONY: test run record image apply

test:
	python3 -m pytest -q

run:
	GGR_DATA_DIR=./data GGR_CONFIG=./config/default.yaml python3 -m uvicorn app.main:app --reload --port 8080

record:
	GGR_DATA_DIR=./data GGR_CONFIG=./config/default.yaml python3 -m recorder.session --once

image:
	docker build -t ggr-vacations:local .

apply:
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -k k8s
