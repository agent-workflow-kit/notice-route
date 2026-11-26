.PHONY: verify live test lint fmt fmt-check typecheck mdlint

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

live:
	@test "$$NOTICE_ROUTE_LIVE" = "1" || { echo "make live is opt-in and sends real test messages: read docs/live-testing.md, then run NOTICE_ROUTE_LIVE=1 make live" >&2; exit 2; }
	@uv run python -m tests.live.plan
	uv run pytest -m live tests/live -s -rs -v --tb=short -p no:cacheprovider

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .
