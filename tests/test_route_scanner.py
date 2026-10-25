"""Route scanner tests — 5 languages (go/java/python/js/ts).

Each language gets a fixture file written to a temp dir; we assert the
``(METHOD, path)`` set the scanner extracts. Heuristic scanner → tests pin the
*common* declaration forms, not exotic ones.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.importers.route_scanner import scan  # noqa: E402


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _pairs(root: Path) -> set[tuple[str, str]]:
    return {(r.method, r.path) for r in scan(root)}


def test_python_fastapi_flask_django(tmp_path: Path):
    _write(tmp_path, "app/api.py", """
from fastapi import FastAPI
app = FastAPI()

@app.get("/api/orders")
async def list_orders():
    ...

@app.post("/api/orders/{order_id}/pay")
def pay_order(order_id: str):
    ...
""")
    _write(tmp_path, "app/flask_app.py", """
@app.route("/api/login", methods=["POST"])
def login():
    ...

@app.route("/health")
def health():
    ...
""")
    _write(tmp_path, "app/urls.py", """
urlpatterns = [
    path("users/<int:uid>/", views.user_detail),
    re_path(r"^reports/$", views.reports),
]
""")
    got = _pairs(tmp_path)
    assert ("GET", "/api/orders") in got
    assert ("POST", "/api/orders/{order_id}/pay") in got
    assert ("POST", "/api/login") in got
    assert ("GET", "/health") in got
    assert ("GET", "/users/{uid}") in got
    assert ("GET", "/reports") in got


def test_js_express_nestjs(tmp_path: Path):
    _write(tmp_path, "src/routes.js", """
const router = require('express').Router()
router.get('/api/users', listUsers)
router.post('/api/users/:id', updateUser)
app.delete('/api/users/:id', removeUser)
""")
    _write(tmp_path, "src/user.controller.ts", """
@Controller('/api/v1/users')
export class UserController {
  @Get()
  findAll() {}

  @Post('/:id/roles')
  assignRole() {}
}
""")
    got = _pairs(tmp_path)
    assert ("GET", "/api/users") in got
    assert ("POST", "/api/users/{id}") in got
    assert ("DELETE", "/api/users/{id}") in got
    assert ("GET", "/api/v1/users") in got
    assert ("POST", "/api/v1/users/{id}/roles") in got


def test_go_gin_nethttp(tmp_path: Path):
    _write(tmp_path, "main.go", """
package main

func main() {
    r := gin.Default()
    r.GET("/api/ping", ping)
    r.POST("/api/items/:id", updateItem)
    v1 := r.Group("/api/v1")
    v1.DELETE("/items/:id", deleteItem)
    http.HandleFunc("/legacy/health", health)
}
""")
    got = _pairs(tmp_path)
    assert ("GET", "/api/ping") in got
    assert ("POST", "/api/items/{id}") in got
    assert ("DELETE", "/items/{id}") in got
    assert ("GET", "/legacy/health") in got


def test_java_spring(tmp_path: Path):
    _write(tmp_path, "OrderController.java", """
@RestController
@RequestMapping("/api/orders")
public class OrderController {

    @GetMapping("/{id}")
    public Order get(Long id) { return null; }

    @PostMapping
    public Order create(Order o) { return o; }

    @RequestMapping(value = "/search", method = RequestMethod.GET)
    public List<Order> search() { return null; }
}
""")
    got = _pairs(tmp_path)
    assert ("GET", "/api/orders/{id}") in got
    assert ("POST", "/api/orders") in got
    assert ("GET", "/api/orders/search") in got


def test_skips_vendored_dirs(tmp_path: Path):
    _write(tmp_path, "node_modules/pkg/index.js", "router.get('/should/not/appear', x)")
    _write(tmp_path, "src/real.js", "router.get('/real', x)")
    got = _pairs(tmp_path)
    assert ("GET", "/real") in got
    assert ("GET", "/should/not/appear") not in got


def test_dedupes_same_route(tmp_path: Path):
    _write(tmp_path, "a.py", '@app.get("/dup")\ndef a(): ...')
    _write(tmp_path, "b.py", '@app.get("/dup")\ndef b(): ...')
    routes = [r for r in scan(tmp_path) if r.path == "/dup"]
    # Same (method, path) in two files: both kept (different files), sorted stable.
    assert len(routes) == 2


def test_module_grouping(tmp_path: Path):
    _write(tmp_path, "a.py", '@app.get("/api/orders/list")\ndef x(): ...')
    routes = scan(tmp_path)
    assert routes[0].module == "orders"


def test_repo_import_dry_run_scans_routes_and_specs(tmp_path: Path):
    """import_from_snapshot 干跑：路由 + OpenAPI 规格都被扫到。"""
    import asyncio

    from testing_platform.importers.repo_import import _find_spec_files, import_from_snapshot

    _write(tmp_path, "src/app.py", '@app.get("/api/orders")\ndef a(): ...')
    _write(tmp_path, "src/r.js", "router.post('/api/users/:id', h)")
    _write(tmp_path, "openapi.json", '{"openapi":"3.0.0","paths":{"/health":{"get":{}}}}')

    assert [p.name for p in _find_spec_files(tmp_path)] == ["openapi.json"]

    res = asyncio.run(import_from_snapshot(tmp_path, "proj-x", dry_run=True))
    assert res["dry_run"] is True
    assert res["routes"] == 2
    assert res["openapi_files"] == 1
    assert res["openapi_endpoints"] == 1
    paths = {(s["method"], s["path"]) for s in res["scanned"]}
    assert ("GET", "/api/orders") in paths
    assert ("POST", "/api/users/{id}") in paths
