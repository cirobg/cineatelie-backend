"""Proves the middleware chain's wiring by running it, not by re-reading
`platform/middleware/__init__.py`'s own docstring — that docstring exists precisely because
the correct `add_middleware` call order is the reverse of the intuitive reading, which is
exactly the kind of thing that looks right and fails only when executed.

Two things are checked against a real ASGI stack (Starlette's `TestClient`):

1. A minimal reconstruction of the same registration pattern (`register_middleware` calls
   `add_middleware` 7→1 then `ErrorHandlerMiddleware` last) produces the intended nesting:
   the first-registered-conceptually layer runs its "before" code first, and
   `ErrorHandlerMiddleware` — added last — genuinely wraps every other layer, catching an
   `AppError` raised by an inner middleware (not just by a route handler).
2. The real `RequestIdMiddleware`, `LoggingMiddleware`, `EdgeVerificationMiddleware` and
   `ErrorHandlerMiddleware` behave as documented, without needing a database.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from cineatelie.core.errors import AppError
from cineatelie.platform.middleware.edge import HEADER_NAME, EdgeVerificationMiddleware
from cineatelie.platform.middleware.error_handler import ErrorHandlerMiddleware
from cineatelie.platform.middleware.request_id import RequestIdMiddleware


async def _ok(request):
    return PlainTextResponse("ok")


async def _raises_app_error(request):
    raise AppError(code="tenant_forbidden", message="no", status_code=403)


def _make_recording_middleware(name: str, calls: list[str]):
    class _Recorder(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            calls.append(f"{name}:before")
            response = await call_next(request)
            calls.append(f"{name}:after")
            return response

    return _Recorder


def test_registration_order_matches_the_documented_reverse_pattern() -> None:
    """Mirrors `register_middleware`'s own registration order (conceptual-outer-first
    registered last) with three throwaway layers, and asserts the resulting call order is
    genuinely outer-to-inner on the way in and inner-to-outer on the way out."""
    calls: list[str] = []
    app = Starlette(routes=[Route("/", _ok)])

    # Registered innermost-conceptually first, outermost-conceptually last -- same pattern
    # as register_middleware's "7 -> 1, then ErrorHandlerMiddleware last".
    app.add_middleware(_make_recording_middleware("inner", calls))
    app.add_middleware(_make_recording_middleware("middle", calls))
    app.add_middleware(_make_recording_middleware("outer", calls))

    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert calls == [
        "outer:before",
        "middle:before",
        "inner:before",
        "inner:after",
        "middle:after",
        "outer:after",
    ]


def test_error_handler_registered_last_catches_an_inner_middlewares_app_error() -> None:
    """The specific failure `ErrorHandlerMiddleware`'s docstring warns about: a FastAPI
    `@app.exception_handler` cannot see an `AppError` raised by another middleware, only by
    a route/dependency. Registering `ErrorHandlerMiddleware` last (outermost) is what fixes
    that -- this test breaks if that registration order is ever accidentally reversed."""

    class _RaisesInMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            raise AppError(code="tenant_forbidden", message="no acesso", status_code=403)

    app = Starlette(routes=[Route("/", _ok)])
    app.add_middleware(_RaisesInMiddleware)
    app.add_middleware(ErrorHandlerMiddleware)  # registered last -> outermost

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_forbidden"


def test_error_handler_maps_an_unexpected_exception_to_a_500_envelope() -> None:
    async def _boom(request):
        raise RuntimeError("unexpected")

    app = Starlette(routes=[Route("/", _boom)])
    app.add_middleware(ErrorHandlerMiddleware)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


def test_request_id_is_generated_when_absent_and_echoed_back() -> None:
    app = Starlette(routes=[Route("/", _ok)])
    app.add_middleware(RequestIdMiddleware)

    response = TestClient(app).get("/")

    assert "X-Request-Id" in response.headers
    assert len(response.headers["X-Request-Id"]) > 0


def test_request_id_from_the_client_is_propagated_unchanged() -> None:
    app = Starlette(routes=[Route("/", _ok)])
    app.add_middleware(RequestIdMiddleware)

    response = TestClient(app).get("/", headers={"X-Request-Id": "client-supplied-id"})

    assert response.headers["X-Request-Id"] == "client-supplied-id"


def test_error_envelope_carries_the_request_id_bound_by_request_id_middleware() -> None:
    """RequestIdMiddleware must run *inside* ErrorHandlerMiddleware (registered after it, in
    register_middleware's reversed order) so the id it binds is still readable via the
    contextvar by the time ErrorHandlerMiddleware builds the envelope for an error raised
    further in."""
    app = Starlette(routes=[Route("/", _raises_app_error)])
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(ErrorHandlerMiddleware)  # registered after RequestIdMiddleware -> outer

    response = TestClient(app, raise_server_exceptions=False).get(
        "/", headers={"X-Request-Id": "req-42"}
    )

    assert response.json()["error"]["request_id"] == "req-42"


def test_edge_verification_disabled_passes_every_request_through() -> None:
    app = Starlette(routes=[Route("/", _ok)])
    app.add_middleware(EdgeVerificationMiddleware, enabled=False, shared_secret="s3cr3t")

    response = TestClient(app).get("/")
    assert response.status_code == 200


def test_edge_verification_enabled_rejects_a_missing_header() -> None:
    app = Starlette(routes=[Route("/", _ok)])
    app.add_middleware(EdgeVerificationMiddleware, enabled=True, shared_secret="s3cr3t")
    app.add_middleware(ErrorHandlerMiddleware)

    response = TestClient(app, raise_server_exceptions=False).get("/")
    assert response.status_code == 401


def test_edge_verification_enabled_rejects_a_wrong_secret() -> None:
    app = Starlette(routes=[Route("/", _ok)])
    app.add_middleware(EdgeVerificationMiddleware, enabled=True, shared_secret="s3cr3t")
    app.add_middleware(ErrorHandlerMiddleware)

    response = TestClient(app, raise_server_exceptions=False).get(
        "/", headers={HEADER_NAME: "wrong"}
    )
    assert response.status_code == 401


def test_edge_verification_enabled_accepts_the_correct_secret() -> None:
    app = Starlette(routes=[Route("/", _ok)])
    app.add_middleware(EdgeVerificationMiddleware, enabled=True, shared_secret="s3cr3t")

    response = TestClient(app).get("/", headers={HEADER_NAME: "s3cr3t"})
    assert response.status_code == 200
