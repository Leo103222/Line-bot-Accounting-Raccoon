# 快速修復指南

## 問題診斷
從日誌可以看到，Google Sheets 連接正常，但 Gemini API 模型名稱有問題。

## ✅ 已修正
- 將 Gemini 模型從 `gemini-1.5-flash` 改為 `gemini-pro`
- `gemini-pro` 是最穩定的模型，支援所有 API 版本

## 🚀 部署步驟

1. **推送修正後的程式碼**
   ```bash
   git add .
   git commit -m "修正 Gemini API 模型名稱"
   git push origin main
   ```

2. **等待 Render 重新部署**
   - 通常需要 2-3 分鐘
   - 檢查部署日誌確認成功

3. **測試 Bot**
   - 發送 `雞排 80` 測試記帳功能
   - 發送 `幫助` 查看功能列表

## 🔍 如果仍有問題

檢查 Render 日誌中的錯誤訊息，常見問題：
- API 金鑰無效
- 模型名稱錯誤
- 網路連接問題

## 📝 測試指令
- `雞排 80` - 記帳測試
- `查帳` - 查看餘額
- `幫助` - 功能說明
