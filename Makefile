.DEFAULT_GOAL := help
UV ?= uv
NPM ?= npm

.PHONY: help setup serve dev web test lint check eval eval-browser readout doctor docker-sandbox clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install Python + web dependencies and build the UI
	$(UV) sync
	cd web && $(NPM) install && $(NPM) run build

serve: ## Run the API server + built UI on http://127.0.0.1:8787
	$(UV) run computeruse serve

dev: ## Hot-reloading UI dev server on :5173 (proxies /api to :8787; run `make serve` too)
	cd web && $(NPM) run dev

web: ## Rebuild the production UI bundle
	cd web && $(NPM) run build

test: ## Python unit/integration tests
	$(UV) run pytest -q

lint: ## Ruff + TypeScript type-check
	$(UV) run ruff check .
	cd web && npx tsc --noEmit

check: lint test ## Lint and test

eval: ## Deterministic eval suite on the simulated desktop (no API key needed)
	$(UV) run computeruse eval --suite simulated-basics --model scripted --concurrency 4

eval-browser: ## Smoke eval on real Chrome (needs Xvfb + Chrome)
	$(UV) run computeruse eval --suite browser-smoke --model scripted --concurrency 2

readout: ## Print the product-metrics readout for the last 7 days
	$(UV) run computeruse readout --since-days 7

doctor: ## Check which computer backends are usable on this machine
	$(UV) run computeruse doctor

docker-sandbox: ## Build the disposable sandbox VM image (Xvfb + Chromium + daemon)
	docker build -t computeruse-sandbox:latest -f docker/Dockerfile .

clean: ## Remove build artefacts (keeps data/)
	rm -rf web/dist .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
