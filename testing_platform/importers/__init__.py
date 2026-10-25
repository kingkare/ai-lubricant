"""接口导入（swagger/apifox/postman/har 等）：把接口文档变成平台实体。

* :mod:`~testing_platform.importers.openapi_importer` —— 解析 OpenAPI 3.0
  JSON/YAML（纯 Python，不引三方），为每个 endpoint 生成一条
  ``mc_test_transport`` 记录。
* :mod:`~testing_platform.importers.spec_normalizer` —— **多格式归一**：
  Swagger 2.0 / Postman Collection / HAR → OpenAPI 3.0（供接口实体导入器通吃）。
* :mod:`~testing_platform.importers.openapi_api_importer` —— 归一后的 OpenAPI 3.0
  → ``mc_test_api`` 接口实体（全字段 + 目录派生 + dry-run + 分页进度）。
* :mod:`~testing_platform.importers.import_jobs` —— 导入任务执行器 + 定时调度
  （``mc_test_api_import_job``：可配置的定时导入）。
* :mod:`~testing_platform.importers.route_scanner` —— 从代码快照扫 HTTP 路由
  （go/java/python/js/ts）。
* :mod:`~testing_platform.importers.repo_import` —— 扫描 + OpenAPI 规格 → 传输层。
"""
from .openapi_importer import (
    OpenAPIImportError,
    fetch_spec_url,
    import_openapi,
    load_document,
    load_openapi_spec,
)
from .openapi_api_importer import (
    build_folder_tree,
    extract_endpoints,
    import_openapi_to_apis,
    tag_folder_path,
)
from .import_jobs import (
    resolve_job_spec,
    run_import_job,
    start_import_scheduler,
    stop_import_scheduler,
)
from .repo_import import import_from_snapshot
from .route_scanner import Route, scan as scan_routes
from .spec_normalizer import (
    FORMATS,
    apifox_to_openapi3,
    detect_format,
    har_to_openapi3,
    normalize_spec,
    postman_to_openapi3,
    swagger2_to_openapi3,
)

__all__ = [
    "FORMATS",
    "OpenAPIImportError",
    "Route",
    "apifox_to_openapi3",
    "build_folder_tree",
    "detect_format",
    "extract_endpoints",
    "fetch_spec_url",
    "har_to_openapi3",
    "import_from_snapshot",
    "import_openapi",
    "import_openapi_to_apis",
    "load_document",
    "load_openapi_spec",
    "normalize_spec",
    "postman_to_openapi3",
    "resolve_job_spec",
    "run_import_job",
    "scan_routes",
    "start_import_scheduler",
    "stop_import_scheduler",
    "swagger2_to_openapi3",
    "tag_folder_path",
]