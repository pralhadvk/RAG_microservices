.PHONY: help kind-create kind-delete secrets build load deploy undeploy seed \
	logs jaeger grafana prometheus status teardown wait-ready load-test

CLUSTER_NAME ?= rag-microservices
NAMESPACE ?= rag-system
OBS_NAMESPACE ?= observability
IMAGE_TAG ?= latest

# image name => service directory
# Format: <image>:<dir>
IMAGES := \
	rag-api-gateway:api-gateway \
	rag-orchestrator:rag-orchestrator \
	rag-ingestion-service:ingestion-service \
	rag-retrieval-service:retrieval-service \
	rag-llm-gateway:llm-gateway \
	rag-chat-ui:chat-ui

help:
	@echo "RAG Microservices — kind local platform"
	@echo ""
	@echo "  make kind-create   Create kind cluster + ingress + observability"
	@echo "  make secrets       Create API key secrets from .env"
	@echo "  make build         Build all service Docker images"
	@echo "  make load          Load images into kind"
	@echo "  make deploy        Apply Kustomize overlay (kind)"
	@echo "  make wait-ready    Wait for all pods to be ready"
	@echo "  make seed          Ingest sample documents"
	@echo "  make status        Show pods/services/ingress"
	@echo "  make logs SERVICE=x  Tail logs for a service"
	@echo "  make jaeger        Port-forward Jaeger UI (:16686)"
	@echo "  make grafana       Port-forward Grafana (:3000)"
	@echo "  make prometheus    Port-forward Prometheus (:9090)"
	@echo "  make load-test     Run hey load test (observe HPA)"
	@echo "  make undeploy      Delete rag-system resources"
	@echo "  make teardown      Delete kind cluster"

kind-create:
	@bash scripts/setup-kind.sh

kind-delete teardown:
	kind delete cluster --name $(CLUSTER_NAME) || true

secrets:
	@test -f .env || (echo "Create .env from .env.example first"; exit 1)
	@bash scripts/create-secrets.sh

build:
	@for pair in $(IMAGES); do \
		img=$${pair%%:*}; \
		dir=$${pair##*:}; \
		echo "Building $$img from services/$$dir..."; \
		docker build -t $$img:$(IMAGE_TAG) \
			-f services/$$dir/Dockerfile \
			. || exit 1; \
	done

load:
	@bash scripts/load-images.sh $(IMAGE_TAG)

deploy:
	kubectl apply -k deploy/overlays/kind
	@echo "Deployed. Run 'make wait-ready' then 'make seed'."

wait-ready:
	kubectl -n $(NAMESPACE) rollout status statefulset/weaviate --timeout=180s || true
	kubectl -n $(NAMESPACE) wait --for=condition=available --timeout=180s deployment --all || true
	kubectl -n $(OBS_NAMESPACE) wait --for=condition=available --timeout=180s deployment --all || true
	@echo "Pods:"
	@kubectl get pods -n $(NAMESPACE)
	@kubectl get pods -n $(OBS_NAMESPACE)

undeploy:
	kubectl delete -k deploy/overlays/kind --ignore-not-found=true || true

seed:
	@bash scripts/seed-data.sh

status:
	@echo "=== rag-system ==="
	kubectl get all,ingress,hpa -n $(NAMESPACE)
	@echo ""
	@echo "=== observability ==="
	kubectl get all,ingress -n $(OBS_NAMESPACE)

logs:
	@test -n "$(SERVICE)" || (echo "Usage: make logs SERVICE=rag-orchestrator"; exit 1)
	kubectl -n $(NAMESPACE) logs -l app=$(SERVICE) -f --tail=100

jaeger:
	@echo "Jaeger UI -> http://localhost:16686"
	kubectl -n $(OBS_NAMESPACE) port-forward svc/jaeger 16686:16686

grafana:
	@echo "Grafana -> http://localhost:3000 (admin/admin)"
	kubectl -n $(OBS_NAMESPACE) port-forward svc/grafana 3000:3000

prometheus:
	@echo "Prometheus -> http://localhost:9090"
	kubectl -n $(OBS_NAMESPACE) port-forward svc/prometheus 9090:9090

load-test:
	@bash scripts/load-test.sh
