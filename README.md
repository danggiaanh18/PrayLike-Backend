# Amen API

Amen API 是一套以 FastAPI 打造的內容服務，支援貼文發布、留言互動、見證紀錄，以及即將推出的 Y 幣獎勵機制。預設使用 SQLite 進行本機開發，亦可透過調整 SQLAlchemy 的連線設定改接其他資料庫。

## 特色
- 貼文支援分類與自動產生的 `docid`，便於前後端溝通。
- 以 `docid` 連結留言與見證，確保資料關聯一致。
- 分頁查詢可依使用者、事件類型、分類進行濾選。
- 以 Alembic 版本化資料庫 schema，模型變更可自動產生遷移腳本。
- 預設 SQLite，本機/雲端皆可透過 `DATABASE_URL` 切換其他資料庫。

## 專案結構
- `main.py`：啟動 FastAPI、設定 CORS、掛載路由。
- `api.py`：定義貼文、留言、見證及查詢等 HTTP 介面。
- `database.py`：負責 SQLAlchemy Engine / Session 建立與 SQLite 權限處理。
- `models.py`：SQLAlchemy ORM 模型（目前為 `Post`）及時區工具函式。
- `run.sh`：啟動腳本，會先啟用 `.venv` 再以 Uvicorn 執行服務。

## 系統需求
- Python 3.10 以上
- `uv`（推薦）或 `pip`
- macOS / Linux Shell 環境（使用 `run.sh` 時）

## 本機環境設置
1. 建立並啟用虛擬環境：
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```
2. 依鎖定清單安裝套件：
   ```bash
   uv pip sync uv.lock
   ```
   若無法使用 `uv`，可改用：
   ```bash
   pip install fastapi uvicorn sqlalchemy pytz
   ```
3. 套用資料庫遷移：
   ```bash
   uv run alembic upgrade head
   ```
   若使用非預設的 SQLite，請先設定 `DATABASE_URL`。

## 執行服務
- 開發模式（自動重新載入）：
  ```bash
  uvicorn main:app --reload
  ```
- 部署類型啟動（啟用 `.venv`，監聽 `0.0.0.0:8080`）：
  ```bash
  bash run.sh
  ```

## 環境變數
- `POSTS_DB_PATH`：自訂 SQLite 檔案路徑；相對路徑會以專案根目錄為基準。
- `POSTS_DB_FALLBACK_DIR`：當原始路徑無法寫入時，用來存放備援資料庫的目錄（預設為系統暫存目錄下的 `amen_api/`）。
- `AI_PROVIDER`：預設 AI 供應商（openai/azure/gemini/claude 或 openai-compatible）。
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`：OpenAI 或相容服務設定（亦可用 `AI_COMPAT_BASE_URL`、`AI_COMPAT_API_KEY`、`AI_COMPAT_MODEL` 指向相容端點）。
- `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` / `AZURE_OPENAI_API_KEY`：Azure OpenAI 設定（可選 `AZURE_OPENAI_MODEL`、`AZURE_OPENAI_API_VERSION`）。
- `GEMINI_API_KEY` / `GEMINI_BASE_URL` / `GEMINI_MODEL`：Gemini 設定。
- `CLAUDE_API_KEY` / `CLAUDE_BASE_URL` / `CLAUDE_MODEL`：Claude 設定。
- `AI_SYSTEM_PROMPT`：自訂 AI 預設 system prompt（未提供 system message 時會自動套用）。

## 資料庫說明
- 預設使用 `posts.db`；若有敏感資料，請勿將該檔案提交至版控。
- 變更 schema 時先更新 ORM 模型，再執行 `uv run alembic revision --autogenerate -m "<message>"` 產生遷移，最後 `uv run alembic upgrade head` 套用。
- Alembic 指令會載入環境變數；至少需提供 `DATABASE_URL`、`JWT_SECRET`、`REFRESH_PEPPER`、`OTP_PEPPER`（可放 `.env`，或在命令前加上臨時值）。
- 欲改用其他資料庫（如 Postgres），直接調整 `.env` 中的 `DATABASE_URL`；SQLite 專用的 `connect_args` 只會在 URL 以 `sqlite` 開頭時自動套用。

## API 快速上手
所有端點皆以 `/api` 為前綴。

- 新增貼文：
  ```bash
  curl -X POST http://localhost:8080/api/posts \
    -H 'Content-Type: application/json' \
    -d '{
      "userid": "u123",
      "content": "平安，家人們！",
      "category": "Personal & Family"
    }'
  ```
- 留言（以貼文 `docid` 為關聯）：
  ```bash
  curl -X POST http://localhost:8080/api/comment \
    -H 'Content-Type: application/json' \
    -d '{
      "userid": "u123",
      "content": "一起禱告！",
      "docid": "<post-docid>"
    }'
  ```
- 新增見證：
  ```bash
  curl -X POST http://localhost:8080/api/witness \
    -H 'Content-Type: application/json' \
    -d '{
      "userid": "u456",
      "content": "神的奇妙帶領",
      "parent_docid": "<post-docid>"
    }'
  ```
  見證可同時附上一張圖片（上限 1MB），改用表單上傳：
  ```bash
  curl -X POST http://localhost:8080/api/witness \
    -H 'Authorization: Bearer <access-token>' \
    -F 'content=神的奇妙帶領' \
    -F 'parent_docid=<post-docid>' \
    -F 'file=@/path/to/witness.jpg'
  ```
- 查詢分頁貼文：
  ```bash
  curl 'http://localhost:8080/api/page?page=1&limit=10&event=post'
  ```
- AI 詢答（支援 openai/azure/gemini/claude 及 openai 相容端點）：
  ```bash
  curl -X POST http://localhost:8080/api/ai/chat \
    -H 'Content-Type: application/json' \
    -d '{
      "messages": [
        {"role": "system", "content": "你是一位禱告小幫手"},
        {"role": "user", "content": "今天的禱告重點是什麼？"}
      ]
    }'
  ```

## Auth 相關端點
- 建立/更新個人檔案（`avatar_url` 可選填）：
  ```bash
  curl -X POST http://localhost:8080/auth/profile \
    -H 'Content-Type: application/json' \
    -H 'Cookie: app_at=<access-token>' \
    -d '{"name": "Tester", "user_id": "tester", "avatar_url": "https://example.com/avatar.png"}'
  ```
- 上傳或變更頭貼（可用檔案或 URL）：
  ```bash
  curl -X POST http://localhost:8080/auth/profile/avatar \
    -H 'Cookie: app_at=<access-token>' \
    -F 'file=@/path/to/avatar.png'
  # 或使用外部連結
  curl -X POST http://localhost:8080/auth/profile/avatar \
    -H 'Cookie: app_at=<access-token>' \
    -F 'avatar_url=https://example.com/avatar.png'
  ```
  檔案上傳上限 1MB，超過會回傳 413。
- 支派列表（需登入）：
  ```bash
  curl -X GET http://localhost:8080/auth/profile/tribes \
    -H 'Cookie: app_at=<access-token>'
  ```
- 設定支派（僅限一次）：
  ```bash
  curl -X POST http://localhost:8080/auth/profile/tribe \
    -H 'Content-Type: application/json' \
    -H 'Cookie: app_at=<access-token>' \
    -d '{"tribe": 4}'
  ```
  編號說明：1=流便、2=西緬、3=利未、4=猶大、5=但、6=拿弗他利、7=迦得、8=亞設、9=以薩迦、10=西布倫、11=約瑟、12=便雅憫。

## 測試指南
- 測試檔放在 `tests/` 目錄，命名建議與模組對應（例如 `tests/test_api_posts.py`）。
- 建議使用 FastAPI `TestClient` 搭配記憶體 SQLite (`sqlite:///:memory:`)，避免污染本機 `posts.db`。
- 執行測試：
  ```bash
  pytest
  ```

## 開發規範
- 依 PEP 8 風格撰寫程式碼，使用 4 空白縮排與 snake_case。
- Pydantic Model 命名建議以 `Create`、`Response` 結尾，清楚表達用途。
- Commit 訊息使用 50 字以內的英文祈使句（例如 `Add witness pagination`）。
- 提交 PR 時附上功能範圍、測試結果、資料庫調整或 API 變更等資訊。

## 開發路線
- 建置 Y 幣獎勵模組，記錄各事件的發放與使用者餘額。
- 為現有端點與 Y 幣流程補齊測試。

## 支援管道
若有問題或功能需求，請依團隊流程提交 Issue；緊急部署問題請聯絡營運支援窗口。
