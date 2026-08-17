default: test

# Run all tests: server unit tests + frontend unit tests + integration tests (builds container via openhost harness).
test: test-server test-frontend test-integration

# Server unit tests (pure python, no containers).
test-server:
    uv run pytest tests/test_search.py tests/test_markdown_headers.py tests/test_service_grants.py -v

# Frontend unit tests (vitest).
test-frontend:
    cd frontend && npm test

# Integration tests: builds container, deploys on real local OpenHost router.
# Requires podman (`podman machine start` on macOS).
test-integration *ARGS:
    uv run pytest tests/test_app.py tests/test_editor.py tests/test_service_e2e.py -x -v {{ARGS}}
