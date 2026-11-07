# Agent Fire Drill build. BreakingWeb is fetched at the pinned commit and never modified.
.PHONY: fetch images site browser worker gateway test
fetch:
	./scripts/fetch_breakingweb.sh
site: fetch
	rm -rf build/site-ctx && mkdir -p build/site-ctx && cp -r build/breakingweb build/site-ctx/breakingweb
	cp images/site/afd_site.py images/site/requirements.lock images/site/Dockerfile build/site-ctx/
	docker build -q -t afd/site:1 build/site-ctx
browser:
	docker build -q -t afd/browser:1 images/browser
worker:
	docker build -q -t afd/worker:1 images/worker
gateway:
	docker build -q -t afd/gateway:1 images/gateway
images: site browser worker gateway
test:
	python3 -m unittest tests.test_gateway
