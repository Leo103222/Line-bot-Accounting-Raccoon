# 步驟 1：選擇一個基礎環境 (使用 Python 3.10 的輕量版)
FROM python:3.10-slim

# 步驟 2：設定環境變數，讓 Python 運作更順暢
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# 步驟 3：在容器內建立一個工作資料夾
WORKDIR /app

# 步驟 4：複製 requirements.txt 並安裝所有依賴套件
# (這一步會利用 Docker 的快取，如果 requirements.txt 沒變，未來建置會很快)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 步驟 5：複製你所有的專案程式碼
COPY . .

# 步驟 6：設定 Gunicorn 啟動指令
# Cloud Run 會自動提供 $PORT 環境變數 (通常是 8080)
# Gunicorn 會監聽這個 port
# "main:app" 指的是 main.py 檔案中的 app 物件
EXPOSE 8080
CMD exec gunicorn main:app --bind 0.0.0.0:${PORT:-8080} --workers 1
