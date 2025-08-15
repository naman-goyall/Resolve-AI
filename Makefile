.PHONY: up down logs rebuild

up:
	docker compose up -d --build

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=200

rebuild:
	docker compose build --no-cache

smoke:
	sh -c "curl -sN http://localhost:8082/subscribe > /tmp/resolve_sse.txt & echo $$! > /tmp/sse_pid && sleep 1 && curl -s -X POST http://localhost:8081/ingest -H 'content-type: application/json' -d '{\"source\":\"make\",\"level\":\"INFO\",\"message\":\"hello from make smoke\"}' >/dev/null && sleep 1 && echo 'SSE bytes:' $$(wc -c < /tmp/resolve_sse.txt) && head -n 5 /tmp/resolve_sse.txt && kill $$(cat /tmp/sse_pid) || true"


