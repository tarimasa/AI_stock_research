# webhook_server.py を uvicorn で起動するコンテナ
# Azure Container Apps にデプロイすることを想定

FROM python:3.12-slim

WORKDIR /app

# 依存パッケージのインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ソースコードをコピー
COPY src/ ./src/
COPY config/ ./config/

# 環境変数のデフォルト
ENV DRY_RUN=false
ENV PORT=8000

EXPOSE 8000

# webhook_server を uvicorn で起動
CMD ["uvicorn", "src.webhook_server:app", "--host", "0.0.0.0", "--port", "8000"]
