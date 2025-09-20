from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks.models import MessageEvent, TextMessageContent
from linebot.v3.messaging import ReplyMessageRequest, TextMessage, MessagingApi
from dotenv import load_dotenv
import os
import logging
import gspread
from google.oauth2.service_account import Credentials
import json
import re
import google.generativeai as genai

app = Flask(__name__)

# 配置日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 載入環境變數
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")



# 從環境變數中讀取 Google Sheets 服務帳號金鑰
creds = Credentials.from_service_account_file("service_account.json", scopes=scope)
SERVICE_ACCOUNT_KEY = None
if SERVICE_ACCOUNT_KEY_JSON:
    try:
        SERVICE_ACCOUNT_KEY = json.loads(SERVICE_ACCOUNT_KEY_JSON)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode SERVICE_ACCOUNT_KEY_JSON: {e}")

# 在正式部署前，請務必將金鑰從程式碼中移除，改用 App Engine 環境變數或 Secret Manager
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY]):
    logger.error("關鍵環境變數未設置，請檢查 .env 檔案或 App Engine 設定。")
    if os.getenv('FLASK_ENV') != 'production':
        raise ValueError("本地測試需要設置所有環境變數。")

# 配置 LINE 和 Gemini API 客戶端
handler = WebhookHandler(LINE_CHANNEL_SECRET)
messaging_api = MessagingApi(LINE_CHANNEL_ACCESS_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# Google Sheets 初始化
def get_sheets_client():
    """初始化 Google Sheets 客戶端並返回工作表物件"""
    logger.info("Initializing Google Sheets client")
    if not SERVICE_ACCOUNT_KEY:
        logger.error("Service account key not found.")
        return None
    try:
        scope = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_KEY, scopes=scope)
        client = gspread.authorize(creds)
        return client.open('記帳小浣熊資料庫').sheet1
    except Exception as e:
        logger.error(f"Failed to get sheets client: {e}")
        return None

# Webhook 處理
@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    logger.info("Received webhook request")
    logger.info(f"Webhook body: {body}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature")
        abort(400)
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return 'Internal Server Error', 500
    return 'OK'

# 處理文字訊息
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    logger.info(f"Received message: '{text}' from user '{user_id}'")

    reply_text = "我不太明白您的意思，請輸入「幫助」來查看指令。"
    sheet = get_sheets_client()
    
    record_match = re.match(r'^(.*)\s+(\d+)$', text)
    
    if text == "幫助":
        reply_text = (
            "📌 **記帳小浣熊使用說明🦝**：\n"
            "💸 **記帳**：輸入「項目 金額」，例如「早餐 50」或「收入 1000」\n"
            "   - 可選項目：餐飲、飲料、交通、娛樂、購物、雜項、收入、早餐、午餐、晚餐\n"
            "   - 「早餐」「午餐」「晚餐」會自動記為「餐飲」\n"
            "   - 收入記帳：使用「收入 金額」或在金額前加 + 號\n"
            "📊 **查帳**：輸入「查帳」，查看總支出、收入和淨餘額\n"
            "📅 **月結**：輸入「月結」，一覽當月收支總結\n"
            "🗑️ **刪除**：輸入「刪除」，移除最近一筆記錄\n"
            "❓ **幫助**：輸入「幫助」，重溫此指引\n"
            "💡 **預算**：輸入「設置預算 項目 限額」或「查看預算」"
        )
    elif text == "查帳":
        reply_text = handle_check_balance(sheet)
    elif text == "月結":
        reply_text = handle_monthly_report(sheet)
    elif text == "刪除":
        reply_text = handle_delete_record(sheet, user_id)
    elif record_match:
        category = record_match.group(1).strip()
        amount_str = record_match.group(2)
        reply_text = handle_new_record(sheet, category, amount_str, event.timestamp, user_id)
    else:
        # 如果使用者輸入的不是預設指令，就傳給 Gemini 處理
        try:
            model = genai.GenerativeModel('gemini-1.5-flash')
            prompt = f"使用者說：「{text}」。請用繁體中文，以一個記帳小浣熊的語氣和角色，給予自然且友善的回覆。您可以嘗試理解並回應，但不要提供任何虛假資訊。如果無法理解，請禮貌地表示。"
            response = model.generate_content(prompt)
            reply_text = response.text
        except Exception as e:
            logger.error(f"Gemini API 呼叫失敗：{e}")
            reply_text = "目前我無法處理這個請求，請輸入「幫助」來查看我能做什麼。"

    # 確保 reply_text 是一個字串
    if not isinstance(reply_text, str):
        reply_text = str(reply_text)
        
    logger.info(f"Reply text:\n{reply_text}")
    
    try:
        messaging_api.reply_message(
            reply_token=reply_token,
            messages=[TextMessage(text=reply_text)]
        )
    except Exception as e:
        logger.error(f"Error replying message: {e}", exc_info=True)
        raise

# 處理查帳指令
def handle_check_balance(sheet):
    """處理查帳指令"""
    if not sheet:
        return "查帳失敗：無法連接試算表。"
    try:
        records = sheet.get_all_records()
        total_income = sum(r.get('金額', 0) for r in records if r.get('項目') == '收入')
        total_expense = sum(r.get('金額', 0) for r in records if r.get('項目') != '收入')
        return f"💰 總收入：{total_income} 元\n💸 總支出：{abs(total_expense)} 元\n📈 淨餘額：{total_income + total_expense} 元"
    except Exception as e:
        logger.error(f"查帳失敗：{e}")
        return "查帳失敗：無法讀取試算表。"

# 處理月結指令
def handle_monthly_report(sheet):
    """處理月結指令"""
    if not sheet:
        return "月結失敗：無法連接試算表。"
    return "📅 月結報表：\n（待實現，需根據日期過濾記錄）"

# 處理刪除指令
def handle_delete_record(sheet, user_id):
    """處理刪除指令"""
    if not sheet:
        return "刪除失敗：無法連接試算表。"
    try:
        records = sheet.get_all_records()
        last_record_index = -1
        for i, record in enumerate(reversed(records)):
            if record.get('使用者ID') == user_id:
                last_record_index = len(records) - i
                break
        
        if last_record_index != -1:
            sheet.delete_rows(last_record_index + 1)
            return "🗑️ 已刪除最近一筆記錄。"
        else:
            return "找不到您的記帳記錄可供刪除。"
    except Exception as e:
        logger.error(f"刪除失敗：{e}")
        return "刪除記錄失敗。"

# 處理記帳指令
def handle_new_record(sheet, category, amount_str, timestamp, user_id):
    """處理記帳指令"""
    valid_categories = ['餐飲', '飲料', '交通', '娛樂', '購物', '雜項', '收入', '早餐', '午餐', '晚餐']
    
    if category not in valid_categories:
        return f"無效項目，請使用：{', '.join(valid_categories)}"

    try:
        amount = int(amount_str)
        
        if category == '收入':
            processed_amount = abs(amount)
        elif category in ['早餐', '午餐', '晚餐']:
            processed_amount = -abs(amount)
            category = '餐飲'
        else:
            processed_amount = -abs(amount)
        
        if sheet:
            records = sheet.get_all_records()
            total_balance = sum(r.get('金額', 0) for r in records) + processed_amount
            sheet.append_row([timestamp, category, processed_amount, user_id, ''])
            return f"✅ 已記錄：{category} {abs(processed_amount)} 元\n📈 目前餘額：{total_balance} 元"
        else:
            return "記帳失敗：無法連接試算表。"
    except ValueError:
        return "金額必須為數字，例如「早餐 50」。"
    except Exception as e:
        logger.error(f"記帳失敗：{e}")
        return "記帳失敗：無法寫入試算表。"

if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)