import json
import logging
import os
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.openapi.docs import get_swagger_ui_html  # ← THÊM
from fastapi.responses import HTMLResponse             # ← THÊM
from database import create_tables
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from ycoin.ycoin_router import router as ycoin_router
from routers.auth import register_auth
from module.friend import router as friend_router
from routers.interactions import router as interactions_router
from routers.activities import router as activities_router
from routers.notifications import router as notifications_router
from routers.posts import router as posts_router
from routers.ai import router as ai_router

DEFAULT_ALLOWED_ORIGINS = [
    "https://pray.yalinelena.church:3000",
    "https://pray.yalinelena.church",   # ← THÊM
    "http://localhost:3000",
    "http://localhost:8000",        # ← THÊM
]


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_allowed_origins(raw_origins: str | None) -> list[str]:
    if raw_origins is None or not raw_origins.strip():
        return DEFAULT_ALLOWED_ORIGINS

    origins: list[str] = []
    try:
        loaded = json.loads(raw_origins)
        if isinstance(loaded, list):
            origins = [str(origin).strip() for origin in loaded if str(origin).strip()]
    except json.JSONDecodeError:
        pass

    if not origins:
        origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

    if not origins:
        logging.warning(
            "CORS_ALLOWED_ORIGINS is empty or invalid; using default allowed origins."
        )
        return DEFAULT_ALLOWED_ORIGINS

    return origins


allowed_origins = parse_allowed_origins(os.getenv("CORS_ALLOWED_ORIGINS"))
allow_credentials = parse_bool(os.getenv("CORS_ALLOW_CREDENTIALS"), True)

if "*" in allowed_origins and allow_credentials:
    raise ValueError(
        "Invalid CORS configuration: allow_credentials=True cannot be used with '*' in allow_origins."
    )

create_tables()

app = FastAPI(
    title="CMS API",
    version="1.0.0",
    description="CMS",
    docs_url=None,  # ← THÊM: tắt docs mặc định để dùng custom
    swagger_ui_parameters={
        "persistAuthorization": True,
        "withCredentials": False,
    },
)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "praylike-secret-2026"),
    same_site="lax",
    https_only=False,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

uploads_dir = os.path.join(os.path.dirname(__file__), "uploads")
if not os.path.exists(uploads_dir):
    os.makedirs(uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

app.include_router(posts_router)
app.include_router(interactions_router)
app.include_router(ycoin_router)
app.include_router(friend_router)
app.include_router(activities_router)
app.include_router(notifications_router)
app.include_router(ai_router)
register_auth(app)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=(
            "## Hướng dẫn xác thực\n"
            "1. Gọi `POST /auth/otp/request` → nhập email → nhận OTP qua mail\n"
            "2. Gọi `POST /auth/otp/verify` → nhập email + OTP → copy `app_token`\n"
            "3. Nhấn nút **Authorize 🔒** → paste `app_token` vào ô **Value** → Authorize\n"
            "> ⚠️ Chỉ paste token thuần tuý, **không** gõ thêm chữ `Bearer`"
        ),
        routes=app.routes,
    )

    schema.setdefault("components", {})
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Paste `app_token` từ `/auth/otp/verify` — KHÔNG kèm chữ 'Bearer'",
        }
    }

    PUBLIC_PATHS = {
        "/auth/otp/request",
        "/auth/otp/verify",
        "/auth/session",
        "/auth/providers",
        "/auth/refresh",
        "/auth/logout",
    }
    for path, path_item in schema["paths"].items():
        if path in PUBLIC_PATHS or "{provider}" in path or path == "/":
            continue
        for method_item in path_item.values():
            if isinstance(method_item, dict):
                method_item.setdefault("security", [{"BearerAuth": []}])

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


# ← THÊM: Custom Swagger UI route
@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="CMS API Docs",
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        swagger_ui_parameters={
            "persistAuthorization": True,
            "withCredentials": False,
            "tryItOutEnabled": True,
            "displayRequestDuration": True,
        },
    )


@app.get("/")
def read_root():
    return {
        "message": "貼文系統API啟動",
        "version": "1.0.1",
        "endpoints": {
            "上傳貼文": "POST /api/posts",
            "取得貼文列表": "GET /api/pages",
            "分頁查詢": "GET /api/pages?page=1&limit=10",
            "修改貼文": "PUT /api/posts/{sn}",
            "刪除貼文": "DELETE /api/posts/{sn}",
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
