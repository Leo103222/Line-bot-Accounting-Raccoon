import os
import logging
import re
import json
import gspread
import google.generativeai as genai
import random
from flask import Flask, request, abort
from linebot import WebhookHandler, LineBotApi
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# === 時區設定（可用環境變數 APP_TZ 覆蓋，預設 Asia/Taipei） ===
APP_TZ = os.getenv('APP_TZ', 'Asia/Taipei')
TIMEZONE = ZoneInfo(APP_TZ)


# === 配置日誌 ===
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === 載入環境變數 ===
load_dotenv()

# === 從環境變數讀取金鑰 ===
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", '記帳小浣熊資料庫')
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# === 驗證金鑰是否已載入 ===
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY, GOOGLE_SHEET_ID]):
    logger.error("!!! 關鍵金鑰載入失敗 !!!")
    raise ValueError("金鑰未配置，請檢查 .env 檔案")
else:
    logger.debug("所有金鑰已成功從 .env 載入。")

# === 初始化 Flask 應用程式 ===
app = Flask(__name__)
logger.info("Flask application initialized successfully.")

# === 配置 LINE 與 Gemini API 客戶端 ===
try:
    if not LINE_CHANNEL_ACCESS_TOKEN or not re.match(r'^[A-Za-z0-9+/=]+$', LINE_CHANNEL_ACCESS_TOKEN):
        logger.error("LINE_CHANNEL_ACCESS_TOKEN 格式無效")
        raise ValueError("LINE_CHANNEL_ACCESS_TOKEN 格式無效")
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-2.5-flash-lite')
    
    logger.debug("LINE 和 Gemini API 客戶端初始化成功")
except Exception as e:
    logger.error(f"API 客戶端初始化失敗: {e}", exc_info=True)
    raise

# === Google Sheets 初始化 ===
def get_sheets_workbook():
    """
    初始化 Google Sheets 客戶端並返回工作簿 (Workbook) 物件
    """
    logger.debug("正在初始化 Google Sheets 憑證...")
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if not creds_json:
            logger.error("GOOGLE_CREDENTIALS 未設置或為空")
            raise ValueError("GOOGLE_CREDENTIALS 未設置或為空")
        
        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(creds)
        
        logger.debug(f"成功授權，嘗試開啟試算表 ID：{GOOGLE_SHEET_ID}")
        workbook = client.open_by_key(GOOGLE_SHEET_ID)
        return workbook
        
    except Exception as e:
        logger.error(f"Google Sheets 初始化失敗：{e}", exc_info=True)
        raise

def ensure_worksheets(workbook):
    """
    確保 Google Sheet 中存在 Transactions 和 Budgets 工作表
    """
    logger.debug("檢查並確保 Transactions 和 Budgets 工作表存在...")
    try:
        try:
            trx_sheet = workbook.worksheet('Transactions')
            logger.debug("找到 Transactions 工作表")
            header = trx_sheet.row_values(1)
            if not header:
                 logger.debug("Transactions 工作表為空，正在寫入標頭...")
                 trx_sheet.append_row(['時間', '類別', '金額', '使用者ID', '使用者名稱', '備註'])
                 
        except gspread.exceptions.WorksheetNotFound:
            logger.debug("未找到 Transactions 工作表，正在創建...")
            trx_sheet = workbook.add_worksheet(title='Transactions', rows=1000, cols=10)
            trx_sheet.append_row(['時間', '類別', '金額', '使用者ID', '使用者名稱', '備註'])

        try:
            budget_sheet = workbook.worksheet('Budgets')
            logger.debug("找到 Budgets 工作表")
            header_budget = budget_sheet.row_values(1)
            if not header_budget:
                logger.debug("Budgets 工作表為空，正在寫入標頭...")
                budget_sheet.append_row(['使用者ID', '類別', '限額'])
                
        except gspread.exceptions.WorksheetNotFound:
            logger.debug("未找到 Budgets 工作表，正在創建...")
            budget_sheet = workbook.add_worksheet(title='Budgets', rows=100, cols=5)
            budget_sheet.append_row(['使用者ID', '類別', '限額'])

        return trx_sheet, budget_sheet
    except Exception as e:
        logger.error(f"創建或檢查工作表失敗：{e}", exc_info=True)
        return None, None

def get_user_profile_name(user_id):
    """
    透過 LINE API 獲取使用者名稱
    """
    logger.debug(f"獲取使用者 {user_id} 的個人資料...")
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except LineBotApiError as e:
        logger.error(f"無法獲取使用者 {user_id} 的個人資料：{e}", exc_info=True)
        return "未知用戶"

# === Webhook 處理 (LINE 訊息的入口) ===
@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    logger.debug(f"Received webhook request, body (前100字): {body[:100]}...")
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError as e:
        logger.error(f"Invalid signature: {e}. Check LINE_CHANNEL_SECRET.", exc_info=True)
        abort(400)
    except Exception as e:
        logger.error(f"Webhook 處理失敗: {e}", exc_info=True)
        return 'Internal Server Error', 500
    
    return 'OK'

# === 訊息總機 (核心邏輯) ===
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    line_timestamp_ms = event.timestamp
    # event_time 就是「傳送時間」
    event_time = datetime.fromtimestamp(line_timestamp_ms / 1000.0, tz=TIMEZONE)
    
    logger.debug(f"Received message: '{text}' from user '{user_id}' at {event_time}")
    logger.debug(f"事件時間 (tz={APP_TZ})：{event_time.isoformat()}")
    
    # 1. 幫助指令 (優先)
    if text == "幫助":
        reply_text = (
            "📌 **記帳小浣熊使用說明🦝**：\n\n"
            "💸 **自然記帳** (AI會幫你分析)：\n"
            "   - 「今天中午吃了雞排80」\n"
            "   - 「昨天喝飲料 50」\n"
            "   - 「16:22 買零食 100」\n"
            "   - 「午餐100 晚餐200」\n"
            "   - 「水果條59x2 + 奶茶35」\n\n"
            "📊 **分析查詢**：\n"
            "   - 「查帳」：查看總支出、收入和淨餘額\n"
            "   - 「月結」：分析這個月的收支總結\n"
            "   - 「本週重點」：分析本週的支出類別\n"
            "   - 「總收支分析」：分析所有時間的支出類別\n\n"
            "🔎 **自然語言查詢**：\n"
            "   - 「查詢 雞排」\n"
            "   - 「查詢 這禮拜的餐飲」\n"
            "   - 「查詢 上個月的收入」\n"
            "   - 「我本月花太多嗎？」\n"
            "   - 「我還剩多少預算？」\n\n"
            "🗑️ **刪除**：\n"
            "   - 「刪除」：(安全) 移除您最近一筆記錄\n"
            "   - 「刪除 雞排」：(危險) 刪除所有含 '雞排' 的記錄\n"
            "   - 「刪除 昨天」：(危險) 刪除所有昨天的記錄\n\n"
            "💡 **預算**：\n"
            "   - 「設置預算 餐飲 3000」\n"
            "   - 「查看預算」：檢查本月預算使用情況\n\n"
            "ℹ️ **其他**：\n"
            "   - 「有哪些類別？」：查看所有記帳項目\n"
            " 類別: 🍽️ 餐飲 🥤 飲料 🚌 交通 🎬 娛樂 🛍️ 購物 🧴 日用品 💡 雜項💰 收入"
        )
        
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
            return
        except LineBotApiError as e:
            logger.error(f"回覆 '幫助' 訊息失敗：{e}", exc_info=True)
            return

    # 2. 獲取 Google Sheets 工作簿 (所有後續指令都需要)
    try:
        workbook = get_sheets_workbook()
        if not workbook:
            raise ValueError("Google Sheets 工作簿為 None")
    except Exception as e:
        logger.error(f"初始化 Google Sheets 失敗：{e}", exc_info=True)
        reply_text = f"糟糕！小浣熊的帳本連接失敗：{str(e)}"
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        except LineBotApiError as e_reply:
            logger.error(f"回覆 Google Sheets 錯誤訊息失敗：{e_reply}", exc_info=True)
        return

    # 確保工作表存在
    trx_sheet, budget_sheet = ensure_worksheets(workbook)
    if not trx_sheet or not budget_sheet:
        reply_text = "糟糕！無法創建或存取 'Transactions' 或 'Budgets' 工作表。"
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        except LineBotApiError as e:
            logger.error(f"回覆工作表錯誤訊息失敗：{e}", exc_info=True)
        return
        
    # === 3. 指令路由器 (Router) ===
    # "明確" 的指令放前面
    try:
        # 3.1 基礎指令
        if text == "查帳": # "查帳" 保持 == 即可
            reply_text = handle_check_balance(trx_sheet, user_id)
        
        elif text.startswith("月結"): # 月結
            reply_text = handle_monthly_report(trx_sheet, user_id, event_time)
        
        # === 關鍵修正：同時檢查 "週" 和 "周" ===
        elif text.startswith("本週重點") or text.startswith("本周重點"):
            reply_text = handle_weekly_report(trx_sheet, user_id, event_time)
        
        elif text.startswith("總收支分析"): # 總收支分析
            reply_text = handle_total_analysis(trx_sheet, user_id)
        
        # 3.2 預算指令 
        elif text.startswith("設置預算"):
            reply_text = handle_set_budget(budget_sheet, text, user_id)
        
        elif text.startswith("查看預算"): 
            reply_text = handle_view_budget(trx_sheet, budget_sheet, user_id, event_time)
        
        # 3.3 刪除指令 
        elif text == "刪除":
            reply_text = handle_delete_last_record(trx_sheet, user_id)
        elif text.startswith("刪除"):
            query_text = text[2:].strip()
            if not query_text:
                reply_text = "請輸入您想刪除的關鍵字喔！\n例如：「刪除 雞排」或「刪除 昨天」"
            else:
                reply_text = handle_advanced_delete(trx_sheet, user_id, query_text, event_time)
                
        # 3.4 查詢指令 
        elif text.startswith("查詢"):
            keyword = text[2:].strip()
            if not keyword:
                reply_text = "請輸入您想查詢的關鍵字喔！\n例如：「查詢 雞排」或「查詢 這禮拜」"
            else:
                reply_text = handle_search_records(trx_sheet, user_id, keyword, event_time)

        # 3.5 預設：NLP 自然語言處理 (記帳, 閒聊, 分析查詢)
        else:
            user_name = get_user_profile_name(user_id)
            # 把「傳送時間」 event_time 傳下去
            reply_text = handle_nlp_record(trx_sheet, budget_sheet, text, user_id, user_name, event_time)

    except Exception as e:
        logger.error(f"處理指令 '{text}' 失敗：{e}", exc_info=True)
        reply_text = f"糟糕！小浣熊處理您的指令時出錯了：{str(e)}"

    # 最終回覆
    if not isinstance(reply_text, str):
        reply_text = str(reply_text)

    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
    except LineBotApiError as e:
        logger.error(f"回覆訊息失敗：{e}", exc_info=True)

# === 核心功能函式 (Helper Functions) ===

def get_datetime_from_record(r):
    """
    相容性輔助函式：
    優先嘗試讀取 '時間' (新)，如果沒有，再讀取 '日期' (舊)
    """
    return r.get('時間', r.get('日期', ''))

def get_cute_reply(category):
    """
    根據類別返回客製化的可愛回應 (隨機)
    """
    replies = {
        "餐飲": [
            "好好吃飯，才有力氣！ 🍜 (⁎⁍̴̛ᴗ⁍̴̛⁎)",
            "吃飽飽，心情好！ 😋",
            "這餐看起來真不錯！ 🍔",
            "美味 +1！ 🍕",
            "享受美食的時光～ 🍰"
        ],
        "飲料": [
            "是全糖嗎？ 🧋 快樂水 get daze！",
            "乾杯！ 🥂",
            "喝點飲料，放鬆一下～ 🥤",
            "是咖啡還是茶？ ☕",
            "續命水來啦！ 💧"
        ],
        "交通": [
            "嗶嗶！出門平安 🚗 目的地就在前方！",
            "出發！ 🚀",
            "路上小心喔！ 🚌",
            "通勤辛苦了！ 🚲",
            "讀萬卷書，行萬里路！ 🌍"
        ],
        "娛樂": [
            "哇！聽起來好好玩！ 🎮 (≧▽≦)",
            "Happy time! 🥳",
            "這錢花得值得！ 🎬",
            "充實生活，讚！ 🎭",
            "放鬆是為了走更長遠的路！ 💖"
        ],
        "購物": [
            "又要拆包裹啦！📦 快樂就是這麼樸實無華！",
            "買！都買！ 🛍️",
            "錢沒有不見，只是變成你喜歡的樣子！ 💸",
            "犒賞一下自己是應該的！ 🎁",
            "新夥伴 get！ 🤖"
        ],
        "日用品": [
            "生活小物補貨完成～ 🧻",
            "家裡又多了一點安全感 ✨",
            "補貨行動成功！🧴",
            "日用品補起來！保持乾淨整潔～ 🧽",
            "小浣熊也喜歡乾乾淨淨的生活！ 🧼"
        ],

        "雜項": [
            "嗯... 這筆花費有點神秘喔 🧐",
            "生活總有些意想不到的開銷～ 🤷",
            "筆記筆記... 📝",
            "OK，記下了！ ✍️",
            "這又是啥？ 😅"
        ],
        "收入": [
            "太棒了！💰 距離財富自由又近了一步！",
            "發財啦！ 🤑",
            "努力有回報！ 💪",
            "錢錢進來！ 🧧",
            "被動收入嗎？真好！ 📈"
        ]
    }
    default_replies = ["✅ 記錄完成！", "OK！記好囉！ ✍️", "小浣熊收到！ 🦝"]
    
    category_replies = replies.get(category, default_replies)
    return random.choice(category_replies)

def check_budget_warning(trx_sheet, budget_sheet, user_id, category, event_time):
    """
    檢查特定類別的預算，如果接近或超過則回傳警告訊息
    """
    if category == "收入":
        return ""

    logger.debug(f"正在為 {user_id} 檢查 {category} 的預算...")
    try:
        budgets_records = budget_sheet.get_all_records()
        user_budget_limit = 0.0
        for b in budgets_records:
            if b.get('使用者ID') == user_id and b.get('類別') == category:
                user_budget_limit = float(b.get('限額', 0))
                break
        
        if user_budget_limit <= 0:
            return "" # 未設定預算

        transactions_records = trx_sheet.get_all_records()
        current_month_str = event_time.strftime('%Y-%m')
        spent = 0.0
        for r in transactions_records:
            try:
                amount = float(r.get('金額', 0))
                record_time_str = get_datetime_from_record(r)
                
                if (r.get('使用者ID') == user_id and
                    record_time_str.startswith(current_month_str) and
                    r.get('類別') == category and
                    amount < 0):
                    spent += abs(amount)
            except (ValueError, TypeError):
                continue
        
        # 判斷是否警告
        percentage = (spent / user_budget_limit) * 100
        
        if percentage >= 100:
            return f"\n\n🚨 警告！ {category} 預算已超支 {spent - user_budget_limit:.0f} 元！ 😱"
        elif percentage >= 90:
            remaining = user_budget_limit - spent
            return f"\n\n🔔 注意！ {category} 預算只剩下 {remaining:.0f} 元囉！ (已用 {percentage:.0f}%)"
        
        return ""
    
    except Exception as e:
        logger.error(f"檢查預算警告失敗：{e}", exc_info=True)
        return "\n(檢查預算時發生錯誤)"

# === *** MODIFIED: handle_nlp_record (強力修正時間規則) *** ===
# === 加法/乘法 表達式解析與合併（本地保險機制） ===
import math

def _parse_amount_expr(expr: str):
    """
    嘗試解析簡單的金額運算字串，支援：
      - 加法：180+60+135
      - 乘法：59x2、59*2（大小寫 x/X）
      - 混合：59x2+35、100+20*3
    僅允許數字、+、-、*、x/X、空白與小數點。
    解析失敗回傳 None。
    """
    try:
        # 標準化：x/X -> *、全形＋ -> +（保守處理）
        expr_std = expr.replace('x', '*').replace('X', '*').replace('＋', '+').replace('－', '-').replace('＊', '*')
        if re.fullmatch(r"[0-9\.\+\-\*\s]+", expr_std):
            # 安全評估：僅算術；不允許 //、** 等進階運算，若出現會在 fullmatch 被擋
            return eval(expr_std, {"__builtins__": {}}, {})
    except Exception:
        pass
    return None

def _try_collapse_add_expr_from_text(original_text: str, records: list):
    """
    嘗試判斷輸入是否像「晚餐180+60+135」這種單一品項的加法表達，
    若 AI 回傳多筆同類別記錄，則合併為一筆。
    合併策略：
      1) 從原始文字抓第一段「非數字 prefix」與緊接的「金額表達式」。
      2) 若偵測到 A+B(+C...)，或含乘法的片段，試著運算。
      3) 若 records>=2 且多筆類別相同，則合併為一筆：
         - datetime 用第一筆
         - category 用第一筆
         - amount 的正負依原 records 的符號為準（多數決；預設支出）
         - notes 使用 prefix（去掉結尾空白）
    回傳 (collapsed_records, did_collapse: bool)
    """
    text = original_text.strip()
    # 找到第一個數字的位置，將前面的當 notes 前綴
    m = re.search(r"\d", text)
    if not m:
        return records, False

    prefix = text[:m.start()].strip()  # 例如「晚餐」
    tail = text[m.start():]            # 例如「180+60+135」或「59x2+35」

    # 僅在 tail 符合「運算表達式」時才嘗試
    val = _parse_amount_expr(tail)
    if val is None:
        return records, False

    # 當 AI 已經回傳單筆就不管；多筆時才合併
    if len(records) < 2:
        return records, False

    # 檢查多筆是否為同類別（寬鬆）：
    cats = [r.get("category", "") for r in records]
    same_cat = len(set(cats)) == 1

    if not same_cat:
        return records, False

    # 多數決決定正負（若含正負混雜，預設支出為負數）
    signs = [1 if float(r.get("amount", 0)) > 0 else -1 for r in records]
    sign = 1 if signs.count(1) > signs.count(-1) else -1

    collapsed = [{
        "datetime": records[0].get("datetime"),
        "category": records[0].get("category"),
        "amount": float(val) * sign,
        "notes": prefix or records[0].get("notes", "")
    }]
    return collapsed, True
def handle_nlp_record(sheet, budget_sheet, text, user_id, user_name, event_time):
    """
    使用 Gemini NLP 處理自然語言記帳 (記帳、聊天、查詢、系統問題)
    event_time 是使用者「傳送訊息」的準確時間。
    """
    logger.debug(f"處理自然語言記帳指令：{text}")
    
    # current_time_str 現在代表「使用者傳送訊息的時間」
    current_time_str = event_time.strftime('%Y-%m-%d %H:%M:%S')
    today_str = event_time.strftime('%Y-%m-%d')
    
    date_context_lines = [
        f"今天是 {today_str} (星期{event_time.weekday()})。",
        # 這裡的 "目前時間" 就是 "傳送時間"
        f"使用者傳送時間是: {event_time.strftime('%H:%M:%S')}",
        "日期參考：",
        f"- 昨天: {(event_time.date() - timedelta(days=1)).strftime('%Y-%m-%d')}"
    ]
    date_context = "\n".join(date_context_lines)
    
    prompt = f"""
    你是一個記帳機器人的 AI 助手，你的名字是「記帳小浣熊🦝」。
    使用者的輸入是：「{text}」
    
    目前的日期時間上下文如下：
    {date_context}
    
    **使用者的「傳送時間」是 {current_time_str}**。

    請嚴格按照以下 JSON 格式回傳，不要有任何其他文字或 "```json" 標記：
    {{
      "status": "success" | "failure" | "chat" | "query" | "system_query",
      "data": [
        {{
          "datetime": "YYYY-MM-DD HH:MM:SS",
          "category": "餐飲" | "飲料" | "交通" | "娛樂" | "購物" | "日用品" | "雜項" | "收入",
          "amount": <number>,
          "notes": "<string>"
        }}
      ] | null,
      "message": "<string>"
    }}

    解析規則：
    1. status "success": 如果成功解析為記帳 (包含一筆或多筆)。
        - data: 必須是一個 "列表" (List)，包含一或多個記帳物件。
        - **多筆記帳**: 如果使用者一次輸入多筆 (例如 "午餐100 晚餐200")，"data" 列表中必須包含 *多個* 物件。
        
        - **時間規則 (非常重要！請嚴格遵守！)**:
            - **(規則 1) 顯式時間 (最高優先)**: 如果使用者 "明確" 提到 "日期" (例如 "昨天", "10/25") 或 "時間" (例如 "16:22", "晚上7點")，**必須** 優先解析並使用該時間。
            - **(規則 2) 預設為傳送時間 (次高優先)**: 如果 "規則 1" 不適用 (即使用者 "沒有" 提到明確日期或時間，例如輸入 "雞排 80", "零食 50")，**必須** 使用使用者的「傳送時間」，即 **{current_time_str}**。
            - **(規則 3) 時段關鍵字 (僅供參考)**: 
                - 如果使用者輸入 "早餐 50"，且「傳送時間」是 09:30，則判斷為補記帳，使用 {today_str} 08:00:00。
                - 如果使用者輸入 "午餐 100"，且「傳送時間」是 14:00，則判斷為補記帳，使用 {today_str} 12:00:00。
                - 如果使用者輸入 "下午茶 100"，且「傳送時間」是 19:36，**此時「傳送時間」(19:36) 與 "下午茶" (15:00) 差距過大，應判斷 "下午茶" 只是「備註」，套用 "規則 2"，必須使用 {current_time_str}**。
                - "晚餐" (18:00), "宵夜" (23:00) 邏輯同上。

        - category: 必須是 [餐飲, 飲料, 交通, 娛樂, 購物, 日用品, 雜項, 收入] 之一。
        - amount: 支出必須為負數 (-)，收入必須為正數 (+)。
        - notes: 盡可能擷取出花費的項目。
        - message: "記錄成功" (此欄位在 success 時不重要)

    2. status "chat": 如果使用者只是在閒聊 (例如 "你好", "你是誰", "謝謝")。
    3. status "query": 如果使用者在 "詢問" 關於他帳務的問題 (例如 "我本月花太多嗎？")。
    4. status "system_query": 如果使用者在詢問 "系統功能" 或 "有哪些類別"。
    5. status "failure": 如果看起來像記帳，但缺少關鍵資訊 (例如 "雞排" (沒說金額))。
    
    範例：

    ⚠️ 規則補充：
    - 如果使用者輸入金額中有「+」或「x/＊」符號（例如 "晚餐180+60+135"、"飲料59x2"），
      請將它們視為「單一筆記帳」的運算表達式，**計算總和**後輸出一筆金額，而不是拆成多筆。
      例如：
      輸入: "晚餐180+60+135" -> {"status": "success", "data": [{"datetime": "{today_str} 18:00:00", "category": "餐飲", "amount": -375, "notes": "晚餐"}], "message": "記錄成功"}
      輸入: "飲料59x2" -> {"status": "success", "data": [{"datetime": "{current_time_str}", "category": "飲料", "amount": -118, "notes": "飲料"}], "message": "記錄成功"}
    輸入: "今天中午吃了雞排80" (規則 1) -> {{"status": "success", "data": [{{"datetime": "{today_str} 12:00:00", "category": "餐飲", "amount": -80, "notes": "雞排"}}], "message": "記錄成功"}}
    輸入: "午餐100 晚餐200" (規則 3) -> {{"status": "success", "data": [{{"datetime": "{today_str} 12:00:00", "category": "餐飲", "amount": -100, "notes": "午餐"}}, {{"datetime": "{today_str} 18:00:00", "category": "餐飲", "amount": -200, "notes": "晚餐"}}], "message": "記錄成功"}}
    輸入: "ACE水果條59x2+龜甲萬豆乳紅茶35" (規則 2) -> {{"status": "success", "data": [{{"datetime": "{current_time_str}", "category": "購物", "amount": -118, "notes": "ACE水果條 59x2"}}, {{"datetime": "{current_time_str}", "category": "飲料", "amount": -35, "notes": "龜甲萬豆乳紅茶"}}], "message": "記錄成功"}}
    輸入: "16:22 記帳零食 50" (規則 1) -> {{"status": "success", "data": [{{"datetime": "{today_str} 16:22:00", "category": "雜項", "amount": -50, "notes": "零食"}}], "message": "記錄成功"}}
    
    **重要範例 (使用者回報的錯誤，假設 {current_time_str} 就是使用者提到的時間)**:
    輸入: "記帳零食 50" (假設 {current_time_str} 是 "2025-10-26 16:22:10") (規則 2)
    -> {{"status": "success", "data": [{{"datetime": "2025-10-26 16:22:10", "category": "雜項", "amount": -50, "notes": "零食"}}], "message": "記錄成功"}}
    
    輸入: "下午茶 100" (假設 {current_time_str} 是 "2025-10-26 19:36:00") (規則 3 判斷為備註 -> 套用規則 2)
    -> {{"status": "success", "data": [{{"datetime": "2025-10-26 19:36:00", "category": "餐飲", "amount": -100, "notes": "下午茶"}}], "message": "記錄成功"}}

    輸入: "你好" -> {{"status": "chat", "data": null, "message": "哈囉！我是記帳小浣熊🦝 需要幫忙記帳嗎？還是想聊聊天呀？"}}
    輸入: "我本月花太多嗎？" -> {{"status": "query", "data": null, "message": "我本月花太多嗎？"}}
    輸入: "目前有什麼項目?" -> {{"status": "system_query", "data": null, "message": "請問您是指記帳的「類別」嗎？ 🦝\n預設類別有：🍽️ 餐飲 🥤 飲料 🚌 交通 🎬 娛樂 🛍️ 購物 🧴 日用品 💡 雜項 💰 收入"}}
    輸入: "宵夜" -> {{"status": "failure", "data": null, "message": "🦝？ 宵夜吃了什麼？花了多少錢呢？"}}
    """
    
    try:
        logger.debug("發送 prompt 至 Gemini API")
        response = gemini_model.generate_content(prompt)
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        logger.debug(f"Gemini NLP response: {clean_response}")
        
        data = json.loads(clean_response)
        status = data.get('status')
        message = data.get('message')

        # === MODIFIED: handle_nlp_record (處理 success, system_query, query, chat, failure) ===
        if status == 'success':
            records = data.get('data', [])

            # 嘗試合併像「晚餐180+60+135」這類被誤拆的多筆紀錄
            try:
                records, _did = _try_collapse_add_expr_from_text(text, records)
            except Exception as _e:
                logger.warning(f"合併加法表達式失敗：{_e}")
            if not records:
                return "🦝？ AI 分析成功，但沒有返回任何記錄。"
            
            reply_summary_lines = []
            last_category = "雜項" 
            
            for record in records:
                # AI 回傳的時間字串
                datetime_str = record.get('datetime', current_time_str)
                category = record.get('category', '雜項')
                amount_str = record.get('amount', 0)
                notes = record.get('notes', text)
                
                try:
                    amount = float(amount_str)
                    if amount == 0:
                        reply_summary_lines.append(f"• {notes} ({category}) 金額為 0，已跳過。")
                        continue
                except (ValueError, TypeError):
                    reply_summary_lines.append(f"• {notes} ({category}) 金額 '{amount_str}' 格式錯誤，已跳過。")
                    continue

                sheet.append_row([datetime_str, category, amount, user_id, user_name, notes])
                logger.debug(f"成功寫入 Google Sheet 記錄: {datetime_str}, {category}, {amount}, {notes}")
                
                try:
                    display_time = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
                except ValueError:
                    display_time = datetime_str 
                
                reply_summary_lines.append(f"• {display_time} {notes} ({category}) {abs(amount):.0f} 元")
                last_category = category
            
            logger.debug("所有記錄寫入完畢")

            cute_reply = get_cute_reply(last_category)
            warning_message = check_budget_warning(sheet, budget_sheet, user_id, last_category, event_time)
            
            all_records = sheet.get_all_records()
            user_balance = 0.0
            for r in all_records:
                if r.get('使用者ID') == user_id:
                    try:
                        user_balance += float(r.get('金額', 0))
                    except (ValueError, TypeError):
                        continue
            
            summary_text = "\n".join(reply_summary_lines)
            return (
                f"{cute_reply}\n\n"
                f"📝 **摘要 (共 {len(reply_summary_lines)} 筆)**：\n"
                f"{summary_text}\n\n"
                f"📈 {user_name} 目前總餘額：{user_balance:.0f} 元"
                f"{warning_message}"
            )

        elif status == 'chat':
            # AI 會回傳隨機的可愛訊息
            return message or "你好！我是記帳小浣熊 🦝"
        
        # === *** NEW: 處理 system_query 狀態 *** ===
        elif status == 'system_query':
            # AI 應該已經根據 prompt 生成了完整的回答
            return message or "我可以幫您記帳！ 🦝 預設類別有：餐飲, 飲料, 交通, 娛樂, 購物, 日用品, 雜項, 收入。"
        
        elif status == 'query':
            # AI 偵測到使用者在 "詢問"
            logger.debug(f"NLP 偵測到聊天式查詢 '{text}'，轉交至 handle_conversational_query")
            # 我們直接把 text (原始訊息) 傳過去分析
            return handle_conversational_query(sheet, budget_sheet, text, user_id, event_time)
        
        else: # status == 'failure'
            return message or "🦝？ 抱歉，我聽不懂..."

    except json.JSONDecodeError as e:
        logger.error(f"Gemini NLP JSON 解析失敗: {clean_response}")
        return f"糟糕！AI 分析器暫時罷工了 (JSON解析失敗)：{clean_response}"
    except Exception as e:
        logger.error(f"Gemini API 呼叫或 GSheet 寫入失敗：{e}", exc_info=True)
        return f"目前我無法處理這個請求：{str(e)}"

def handle_check_balance(sheet, user_id):
    """
    處理 '查帳' 指令
    """
    logger.debug(f"處理 '查帳' 指令，user_id: {user_id}")
    try:
        records = sheet.get_all_records()
        user_records = [r for r in records if r.get('使用者ID') == user_id]
        
        if not user_records:
            return "您目前沒有任何記帳記錄喔！"

        total_income = 0.0
        total_expense = 0.0
        
        for r in user_records:
            try:
                amount = float(r.get('金額', 0))
                if amount > 0:
                    total_income += amount
                else:
                    total_expense += amount
            except (ValueError, TypeError):
                continue

        total_balance = total_income + total_expense
        
        return (
            f"📊 **您的財務總覽**：\n\n"
            f"💰 總收入：{total_income:.0f} 元\n"
            f"💸 總支出：{abs(total_expense):.0f} 元\n"
            f"--------------------\n"
            f"📈 淨餘額：{total_balance:.0f} 元"
        )
    except Exception as e:
        logger.error(f"查帳失敗：{e}", exc_info=True)
        return f"查帳失敗：無法讀取試算表：{str(e)}"

def handle_monthly_report(sheet, user_id, event_time):
    """
    處理 '月結' 指令
    """
    logger.debug(f"處理 '月結' 指令，user_id: {user_id}")
    try:
        records = sheet.get_all_records()
        current_month_str = event_time.strftime('%Y-%m')
        
        user_month_records = []
        for r in records:
            record_time_str = get_datetime_from_record(r)
            if (r.get('使用者ID') == user_id and 
                record_time_str.startswith(current_month_str)):
                user_month_records.append(r)
        
        if not user_month_records:
            return f"📅 {current_month_str} 月報表：\n您這個月還沒有任何記錄喔！"

        total_income = 0.0
        total_expense = 0.0
        category_spending = {}

        for r in user_month_records:
            try:
                amount = float(r.get('金額', 0))
                if amount > 0:
                    total_income += amount
                else:
                    total_expense += amount
                    category = r.get('類別', '雜項')
                    category_spending[category] = category_spending.get(category, 0) + abs(amount)
            except (ValueError, TypeError):
                continue
        
        reply = f"📅 **{current_month_str} 月結報表**：\n\n"
        reply += f"💰 本月收入：{total_income:.0f} 元\n"
        reply += f"💸 本月支出：{abs(total_expense):.0f} 元\n"
        reply += f"📈 本月淨利：{total_income + total_expense:.0f} 元\n"
        
        if category_spending:
            reply += "\n--- 支出分析 (花費最多) ---\n"
            sorted_spending = sorted(category_spending.items(), key=lambda item: item[1], reverse=True)
            
            for i, (category, amount) in enumerate(sorted_spending):
                icon = ["🥇", "🥈", "🥉"]
                prefix = icon[i] if i < 3 else "🔹"
                reply += f"{prefix} {category}: {amount:.0f} 元\n"
        
        return reply
    except Exception as e:
        logger.error(f"月結失敗：{e}", exc_info=True)
        return f"月結報表產生失敗：{str(e)}"

def handle_weekly_report(sheet, user_id, event_time):
    """
    處理 '本週重點' 指令
    """
    logger.debug(f"處理 '本週重點' 指令，user_id: {user_id}")
    try:
        records = sheet.get_all_records()
        
        today = event_time.date()
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        
        start_of_week_str = start_of_week.strftime('%Y-%m-%d')
        end_of_week_str = end_of_week.strftime('%Y-%m-%d')

        logger.debug(f"本週區間：{start_of_week_str} 到 {end_of_week_str}")

        user_week_records = []
        for r in records:
            if r.get('使用者ID') != user_id:
                continue
                
            record_time_str = get_datetime_from_record(r)
            if not record_time_str:
                continue
            
            try:
                record_date = datetime.strptime(record_time_str[:10], '%Y-%m-%d').date()
                if start_of_week <= record_date <= end_of_week:
                    user_week_records.append(r)
            except ValueError:
                continue
        
        if not user_week_records:
            return f"📊 本週摘要 ({start_of_week_str} ~ {end_of_week_str})：\n您這週還沒有任何記錄喔！"

        total_expense = 0.0
        category_spending = {}
        day_spending = {} 

        for r in user_week_records:
            try:
                amount = float(r.get('金額', 0))
                if amount < 0:
                    expense = abs(amount)
                    total_expense += expense
                    
                    category = r.get('類別', '雜項')
                    category_spending[category] = category_spending.get(category, 0) + expense
                    
                    record_date_str = get_datetime_from_record(r)[:10]
                    day_spending[record_date_str] = day_spending.get(record_date_str, 0) + expense
                    
            except (ValueError, TypeError):
                continue
        
        reply = f"📊 **本週花費摘要** ({start_of_week_str} ~ {end_of_week_str})：\n"
        reply += f"💸 本週總支出：{total_expense:.0f} 元\n\n"
        
        if category_spending:
            reply += "--- 支出類別 ---\n"
            sorted_spending = sorted(category_spending.items(), key=lambda item: item[1], reverse=True)
            
            for category, amount in sorted_spending:
                percentage = (amount / total_expense) * 100 if total_expense > 0 else 0
                reply += f"• {category}：{amount:.0f} 元 (佔 {percentage:.0f}%)\n"
        
        if day_spending:
            reply += "\n--- 每日花費 ---\n"
            most_spent_day = max(day_spending, key=day_spending.get)
            most_spent_amount = day_spending[most_spent_day]
            
            try:
                display_date = datetime.strptime(most_spent_day, '%Y-%m-%d').strftime('%m/%d')
            except ValueError:
                display_date = most_spent_day
                
            reply += f"📉 花最多的一天：{display_date} (共 {most_spent_amount:.0f} 元)\n"
            
        return reply
    except Exception as e:
        logger.error(f"本週重點失敗：{e}", exc_info=True)
        return f"本週重點報表產生失敗：{str(e)}"

def handle_total_analysis(sheet, user_id):
    """
    處理 '總收支分析' 指令
    """
    logger.debug(f"處理 '總收支分析' 指令，user_id: {user_id}")
    try:
        records = sheet.get_all_records()
        user_records = [r for r in records if r.get('使用者ID') == user_id]
        
        if not user_records:
            return "您目前沒有任何記帳記錄喔！"

        total_income = 0.0
        total_expense = 0.0
        category_spending = {}

        for r in user_records:
            try:
                amount = float(r.get('金額', 0))
                if amount > 0:
                    total_income += amount
                else:
                    expense = abs(amount)
                    total_expense += expense
                    category = r.get('類別', '雜項')
                    category_spending[category] = category_spending.get(category, 0) + expense
            except (ValueError, TypeError):
                continue
        
        reply = f"📈 **您的總收支分析** (從開始記帳至今)：\n\n"
        reply += f"💰 總收入：{total_income:.0f} 元\n"
        reply += f"💸 總支出：{total_expense:.0f} 元\n"
        reply += f"📊 淨餘額：{total_income - total_expense:.0f} 元\n"
        
        if category_spending:
            reply += "\n--- 總支出類別分析 (花費最多) ---\n"
            sorted_spending = sorted(category_spending.items(), key=lambda item: item[1], reverse=True)
            
            for i, (category, amount) in enumerate(sorted_spending):
                percentage = (amount / total_expense) * 100 if total_expense > 0 else 0
                icon = ["🥇", "🥈", "🥉"]
                prefix = icon[i] if i < 3 else "🔹"
                reply += f"{prefix} {category}: {amount:.0f} 元 (佔 {percentage:.1f}%)\n"
        
        return reply
    except Exception as e:
        logger.error(f"總收支分析失败：{e}", exc_info=True)
        return f"總收支分析報表產生失败：{str(e)}"


def handle_delete_last_record(sheet, user_id):
    """
    處理 '刪除' 指令，刪除使用者的 "最後一筆" 記錄
    """
    logger.debug(f"處理 '刪除' (最後一筆) 指令，user_id: {user_id}")
    try:
        all_values = sheet.get_all_values()
        
        if not all_values:
            return "您的帳本是空的，沒有記錄可刪除。"
            
        header = all_values[0]
        try:
            user_id_col_index = header.index('使用者ID')
        except ValueError:
            logger.warning("找不到 '使用者ID' 欄位，預設為 3 (D欄)")
            user_id_col_index = 3 
        
        for row_index in range(len(all_values) - 1, 0, -1): 
            row = all_values[row_index]
            if len(row) > user_id_col_index and row[user_id_col_index] == user_id:
                row_to_delete = row_index + 1
                
                try:
                    amount_val = float(row[2])
                    deleted_desc = f"{row[0]} {row[1]} {amount_val:.0f} 元"
                except (ValueError, TypeError, IndexError):
                    deleted_desc = f"第 {row_to_delete} 行的記錄"
                
                sheet.delete_rows(row_to_delete)
                return f"🗑️ 已刪除：{deleted_desc}"
        
        return "找不到您的記帳記錄可供刪除。"
    except Exception as e:
        logger.error(f"刪除失敗：{e}", exc_info=True)
        return f"刪除記錄失敗：{str(e)}"

# === *** MODIFIED: handle_advanced_delete (增加標頭防錯) *** ===
def handle_advanced_delete(sheet, user_id, query_text, event_time):
    """
    處理進階刪除 (依關鍵字或日期)
    """
    logger.debug(f"處理 '進階刪除' 指令，user_id: {user_id}, query: {query_text}")
    
    try:
        parsed_query = call_search_nlp(query_text, event_time)
        if parsed_query.get('status') == 'failure':
            return parsed_query.get('message', "🦝 刪除失敗，我不太懂您的意思。")

        keyword = parsed_query.get('keyword')
        start_date = parsed_query.get('start_date')
        end_date = parsed_query.get('end_date')
        nlp_message = parsed_query.get('message', f"關於「{query_text}」")

        if not keyword and not start_date and not end_date:
            return f"🦝 刪除失敗：AI 無法解析您的條件「{query_text}」。"
            
    except Exception as e:
        logger.error(f"進階刪除的 NLP 解析失敗：{e}", exc_info=True)
        return f"刪除失敗：AI 分析器出錯：{str(e)}"
        
    logger.debug(f"NLP 解析結果：Keyword: {keyword}, Start: {start_date}, End: {end_date}")

    try:
        all_values = sheet.get_all_values()
        
        if not all_values:
            return "🦝 您的帳本是空的，找不到記錄可刪除。"
            
        header = all_values[0]
        
        # === *** 增加防錯機制 *** ===
        try:
            idx_uid = header.index('使用者ID')
            idx_time = header.index('時間')
            idx_cat = header.index('類別')
            idx_note = header.index('備註')
        except ValueError as e:
            logger.error(f"進階刪除失敗：GSheet 標頭欄位名稱錯誤或缺失: {e}")
            return "刪除失敗：找不到必要的 GSheet 欄位 (例如 '使用者ID', '時間', '類別', '備註')。請檢查 GSheet 標頭是否正確。"
        # === *** 防錯結束 *** ===
        
        rows_to_delete = [] 
        
        start_dt = datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else None
        end_dt = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None
        
        logger.debug("開始遍歷 GSheet Values 尋找刪除目標...")
        
        for row_index in range(1, len(all_values)):
            row = all_values[row_index]
            
            if len(row) <= max(idx_uid, idx_time, idx_cat, idx_note):
                continue
            
            if row[idx_uid] != user_id:
                continue
            
            keyword_match = True
            date_match = True
            
            if keyword:
                keyword_match = (keyword in row[idx_cat]) or (keyword in row[idx_note])
            
            record_datetime_str = row[idx_time]
            if (start_dt or end_dt) and record_datetime_str:
                try:
                    record_dt = datetime.strptime(record_datetime_str[:10], '%Y-%m-%d').date()
                        
                    if start_dt and record_dt < start_dt:
                        date_match = False
                    if end_dt and record_dt > end_dt:
                        date_match = False
                except ValueError:
                    date_match = False
            
            if keyword_match and date_match:
                rows_to_delete.append(row_index + 1)
        
        if not rows_to_delete:
            return f"🦝 找不到符合「{nlp_message}」的記錄可供刪除。"
        
        logger.info(f"準備從後往前刪除 {len(rows_to_delete)} 行: {rows_to_delete}")
        
        deleted_count = 0
        for row_num in sorted(rows_to_delete, reverse=True):
            try:
                sheet.delete_rows(row_num)
                deleted_count += 1
            except Exception as e:
                logger.error(f"刪除第 {row_num} 行失敗: {e}")
                
        return f"🗑️ 刪除完成！\n共刪除了 {deleted_count} 筆關於「{nlp_message}」的記錄。"

    except Exception as e:
        logger.error(f"進階刪除失敗：{e}", exc_info=True)
        return f"刪除記錄失敗：{str(e)}"

def handle_set_budget(sheet, text, user_id):
    """
    處理 '設置預算' 指令
    """
    logger.debug(f"處理 '設置預算' 指令，user_id: {user_id}, text: {text}")
    match = re.match(r'設置預算\s+([\u4e00-\u9fa5]+)\s+(\d+)', text)
    if not match:
        return "格式錯誤！請輸入「設置預算 [類別] [限額]」，例如：「設置預算 餐飲 3000」"
    
    category = match.group(1).strip()
    limit = int(match.group(2)) 
    
    valid_categories = ['餐飲', '飲料', '交通', '娛樂', '購物', '日用品', '雜項']
    if category not in valid_categories:
        return f"無效類別，請使用：{', '.join(valid_categories)}"

    try:
        cell_list = sheet.findall(user_id)
        found_row = -1
        
        for cell in cell_list:
            row_values = sheet.row_values(cell.row)
            if len(row_values) > 1 and row_values[1] == category:
                found_row = cell.row
                break
        
        if found_row != -1:
            sheet.update_cell(found_row, 3, limit) 
            return f"✅ 已更新預算：{category} {limit} 元" 
        else:
            sheet.append_row([user_id, category, limit]) 
            return f"✅ 已設置預算：{category} {limit} 元" 
    except Exception as e:
        logger.error(f"設置預算失敗：{e}", exc_info=True)
        return f"設置預算失敗：{str(e)}"

def handle_view_budget(trx_sheet, budget_sheet, user_id, event_time):
    """
    處理 '查看預算' 指令
    """
    logger.debug(f"處理 '查看預算' 指令，user_id: {user_id}")
    try:
        budgets_records = budget_sheet.get_all_records()
        user_budgets = [b for b in budgets_records if b.get('使用者ID') == user_id]
        
        if not user_budgets:
            return "您尚未設置任何預算。請輸入「設置預算 [類別] [限額]」"

        transactions_records = trx_sheet.get_all_records()
        current_month_str = event_time.strftime('%Y-%m')
        
        user_month_expenses = []
        for r in transactions_records:
            try:
                amount = float(r.get('金額', 0))
                record_time_str = get_datetime_from_record(r)
                
                if (r.get('使用者ID') == user_id and
                    record_time_str.startswith(current_month_str) and
                    amount < 0):
                    user_month_expenses.append(r)
            except (ValueError, TypeError):
                continue

        reply = f"📊 **{current_month_str} 預算狀態**：\n"
        total_spent = 0.0
        total_limit = 0.0
        
        for budget in user_budgets:
            category = budget.get('類別')
            limit = float(budget.get('限額', 0))
            if limit <= 0:
                continue
                
            total_limit += limit
            spent = sum(abs(float(r.get('金額', 0))) for r in user_month_expenses if r.get('類別') == category)
            total_spent += spent
            remaining = limit - spent
            percentage = (spent / limit) * 100 if limit > 0 else 0
            
            bar_fill = '■' * int(percentage / 10)
            bar_empty = '□' * (10 - int(percentage / 10))
            if percentage > 100:
                bar_fill = '■' * 10
                bar_empty = ''
            elif percentage < 0:
                 bar_fill = ''
                 bar_empty = '□' * 10
                 
            status_icon = "🟢" if remaining >= 0 else "🔴"
            reply += f"\n{category} (限額 {limit:.0f} 元)\n"
            reply += f"   {status_icon} 已花費：{spent:.0f} 元\n"
            reply += f"   [{bar_fill}{bar_empty}] {percentage:.0f}%\n"
            reply += f"   剩餘：{remaining:.0f} 元\n"

        reply += "\n--------------------\n"
        if total_limit > 0:
            total_remaining = total_limit - total_spent
            total_percentage = (total_spent / total_limit) * 100
            status_icon = "🟢" if total_remaining >= 0 else "🔴"
            
            reply += f"總預算： {total_limit:.0f} 元\n"
            reply += f"總花費： {total_spent:.0f} 元\n"
            reply += f"{status_icon} 總剩餘：{total_remaining:.0f} 元 ({total_percentage:.0f}%)"
        else:
            reply += "總預算尚未設定或設定為 0。"
        
        return reply
    except Exception as e:
        logger.error(f"查看預算失敗：{e}", exc_info=True)
        return f"查看預算失敗：{str(e)}"

def handle_conversational_query(trx_sheet, budget_sheet, text, user_id, event_time):
    """
    處理聊天式查詢 (例如 "我還剩多少預算？", "我本月花太多嗎？")
    (由 handle_nlp_record 的 "query" 狀態觸發)
    """
    logger.debug(f"處理 '聊天式查詢' 指令，user_id: {user_id}, text: {text}")

    # 情況一：詢問預算
    if any(kw in text for kw in ["預算", "剩多少"]):
        logger.debug("聊天式查詢：轉交至 handle_view_budget")
        return handle_view_budget(trx_sheet, budget_sheet, user_id, event_time)
        
    # 情況二：詢問花費 (例如 "花太多", "跟上月比")
    if any(kw in text for kw in ["花太多", "跟上月比", "花費如何"]):
        logger.debug("聊天式查詢：執行 月 vs 月 比較")
        try:
            # 1. 取得本月資料
            this_month_date = event_time.date()
            this_month_data = get_spending_data_for_month(trx_sheet, user_id, this_month_date.year, this_month_date.month)
            
            # 2. 取得上月資料
            last_month_end_date = this_month_date.replace(day=1) - timedelta(days=1)
            last_month_data = get_spending_data_for_month(trx_sheet, user_id, last_month_end_date.year, last_month_end_date.month)

            this_month_total = this_month_data['total']
            last_month_total = last_month_data['total']
            
            # === 可愛語氣區 ===
            reply_intros = [
                "🦝 幫您分析了一下：\n\n",
                "小浣熊翻了翻帳本... 🧐\n\n",
                "熱騰騰的分析來囉！ (ゝ∀･)b\n\n"
            ]
            reply = random.choice(reply_intros)
            reply += f"• 本月 ({this_month_date.month}月) 目前支出：{this_month_total:.0f} 元\n"
            reply += f"• 上月 ({last_month_end_date.month}月) 總支出：{last_month_total:.0f} 元\n"
            
            if last_month_total > 0:
                percentage_diff = ((this_month_total - last_month_total) / last_month_total) * 100
                
                # === 可愛語氣區 ===
                if percentage_diff > 10: # 花費增加
                    spend_more_replies = [
                        f"📈 哎呀！您本月花費比上月 **多 {percentage_diff:.0f}%**！ 😱",
                        f"📈 注意！您本月花費增加了 {percentage_diff:.0f}%！ 要踩剎車啦 🚗",
                    ]
                    reply += random.choice(spend_more_replies) + "\n"
                elif percentage_diff < -10: # 花費減少
                    spend_less_replies = [
                        f"📉 太棒了！您本月花費比上月 **少 {abs(percentage_diff):.0f}%**！ (≧▽≦)b",
                        f"📉 讚喔！您本月節省了 {abs(percentage_diff):.0f}%！ 繼續保持！ 💪",
                    ]
                    reply += random.choice(spend_less_replies) + "\n"
                else: # 持平
                    reply += f"📊 您本月花費與上月差不多～ (大概 {percentage_diff:+.0f}%)。\n"
            else:
                reply += "📊 上月沒有支出記錄可供比較。\n"

            # 找出差異最大的類別
            category_diff = {}
            all_categories = set(this_month_data['categories'].keys()) | set(last_month_data['categories'].keys())
            
            for category in all_categories:
                this_month_cat = this_month_data['categories'].get(category, 0)
                last_month_cat = last_month_data['categories'].get(category, 0)
                diff = this_month_cat - last_month_cat
                if diff > 0: # 只關心增加的
                    category_diff[category] = diff

            if category_diff:
                most_increased_cat = max(category_diff, key=category_diff.get)
                increase_amount = category_diff[most_increased_cat]
                reply += f"\n💡 **主要差異**：本月 **{most_increased_cat}** 類別的花費增加了 {increase_amount:.0f} 元。"
            
            return reply

        except Exception as e:
            logger.error(f"聊天式查詢失敗：{e}", exc_info=True)
            return f"糟糕！小浣熊分析時打結了：{str(e)}"

    # 如果 AI 判斷是 query，但我們這邊的規則都沒對上
    return random.choice([
        "🦝？ 抱歉，我不太懂您的問題... 試試看「查詢...」或「本週重點」？",
        "嗯... (歪頭) 您的問題有點深奥，小浣熊聽不懂 😅",
        "您可以問我「我本月花太多嗎？」或「我還剩多少預算？」喔！"
    ])

def get_spending_data_for_month(sheet, user_id, year, month):
    """
    獲取特定年/月，某使用者的總支出和分類支出
    """
    logger.debug(f"輔助函式：抓取 {user_id} 在 {year}-{month} 的資料")
    month_str = f"{year}-{month:02d}"
    
    total_expense = 0.0
    category_spending = {}
    
    records = sheet.get_all_records()
    
    for r in records:
        record_time_str = get_datetime_from_record(r)
        if (r.get('使用者ID') == user_id and 
            record_time_str.startswith(month_str)):
            
            try:
                amount = float(r.get('金額', 0))
                if amount < 0:
                    expense = abs(amount)
                    total_expense += expense
                    category = r.get('類別', '雜項')
                    category_spending[category] = category_spending.get(category, 0) + expense
            except (ValueError, TypeError):
                continue
                
    return {"total": total_expense, "categories": category_spending}


def handle_search_records(sheet, user_id, query_text, event_time):
    """
    處理關鍵字和日期區間查詢 (使用 NLP)
    """
    logger.debug(f"處理 '查詢' 指令，user_id: {user_id}, query: {query_text}")

    try:
        parsed_query = call_search_nlp(query_text, event_time)
        if parsed_query.get('status') == 'failure':
            return parsed_query.get('message', "🦝 查詢失敗，我不太懂您的意思。")

        keyword = parsed_query.get('keyword')
        start_date = parsed_query.get('start_date')
        end_date = parsed_query.get('end_date')
        nlp_message = parsed_query.get('message', f"關鍵字「{keyword or ''}」")
            
    except Exception as e:
        logger.error(f"查詢的 NLP 解析失敗：{e}", exc_info=True)
        return f"查詢失敗：AI 分析器出錯：{str(e)}"
        
    logger.debug(f"NLP 解析結果：Keyword: {keyword}, Start: {start_date}, End: {end_date}")

    records = sheet.get_all_records()
    matches = []
    
    try:
        start_dt = datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else None
        end_dt = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None
    except ValueError as e:
        return f"AI 回傳的日期格式錯誤 ({start_date}, {end_date})。"

    for r in records:
        if r.get('使用者ID') != user_id:
            continue
        
        keyword_match = True
        date_match = True
        
        if keyword:
            keyword_match = (keyword in r.get('類別', '')) or (keyword in r.get('備註', ''))
        
        record_datetime_str = get_datetime_from_record(r)
        
        if (start_dt or end_dt) and record_datetime_str:
            try:
                record_dt = datetime.strptime(record_datetime_str[:10], '%Y-%m-%d').date()
                    
                if start_dt and record_dt < start_dt:
                    date_match = False
                if end_dt and record_dt > end_dt:
                    date_match = False
            except ValueError:
                date_match = False 
        
        if keyword_match and date_match:
            matches.append(r)
    
    if not matches:
        return f"🦝 找不到關於「{nlp_message}」的任何記錄喔！"
    
    reply = f"🔎 {nlp_message} (共 {len(matches)} 筆)：\n\n"
    limit = 20 
    
    sorted_matches = sorted(matches, key=lambda x: get_datetime_from_record(x), reverse=True)
    
    total_amount_all_matches = 0.0
    
    for r in sorted_matches:
         try:
            amount = float(r.get('金額', 0))
            total_amount_all_matches += amount
            
            if len(reply.split('\n')) <= limit + 5: 
                category = r.get('類別', 'N/A')
                notes = r.get('備註', 'N/A')
                date_str = get_datetime_from_record(r)
                
                if not date_str:
                     display_date = "N/A"
                else:
                    try:
                        if len(date_str) > 10:
                            display_date = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
                        else:
                            display_date = datetime.strptime(date_str, '%Y-%m-%d').strftime('%Y-%m-%d')
                    except ValueError:
                        display_date = date_str 
                
                reply += f"• {display_date} {notes} ({category}) {amount:.0f} 元\n"
                
         except (ValueError, TypeError):
            continue
    
    reply += f"\n--------------------\n"
    reply += f"📈 查詢總計：{total_amount_all_matches:.0f} 元\n"
    
    if len(matches) > limit:
        reply += f"(僅顯示最近 {limit} 筆記錄)"
        
    return reply

def call_search_nlp(query_text, event_time):
    """
    呼叫 Gemini NLP 來解析 "查詢" 或 "刪除" 的條件
    返回一個 dict: {status, keyword, start_date, end_date, message}
    """
    today = event_time.date()
    today_str = today.strftime('%Y-%m-%d')
    
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    start_of_last_week = start_of_week - timedelta(days=7)
    end_of_last_week = start_of_week - timedelta(days=1)
    
    start_of_month = today.replace(day=1)
    
    last_month_end_date = start_of_month - timedelta(days=1)
    start_of_last_month = last_month_end_date.replace(day=1)

    date_context_lines = [
        f"今天是 {today_str} (星期{today.weekday()})。",
        f"昨天: {(today - timedelta(days=1)).strftime('%Y-%m-%d')}",
        f"本週 (週一到週日): {start_of_week.strftime('%Y-%m-%d')} 到 {end_of_week.strftime('%Y-%m-%d')}",
        f"上週 (週一到週日): {start_of_last_week.strftime('%Y-%m-%d')} 到 {end_of_last_week.strftime('%Y-%m-%d')}",
        f"本月: {start_of_month.strftime('%Y-%m-%d')} 到 {today_str}",
        f"上個月: {start_of_last_month.strftime('%Y-%m-%d')} 到 {last_month_end_date.strftime('%Y-%m-%d')}",
    ]
    date_context = "\n".join(date_context_lines)

    prompt = f"""
    你是一個查詢助手。使用者的查詢是：「{query_text}」
    
    目前的日期上下文如下：
    {date_context}

    請嚴格按照以下 JSON 格式回傳：
    {{
      "status": "success" | "failure",
      "keyword": "<string>" | null,
      "start_date": "YYYY-MM-DD" | null,
      "end_date": "YYYY-MM-DD" | null,
      "message": "<string>"
    }}
    
    解析規則：
    1. status: "success"
    2. keyword: 提取查詢的關鍵字 (例如 "雞排", "餐飲")。如果沒有關鍵字，則為 null。
    3. start_date: 提取查詢的 "起始日期"。
    4. end_date: 提取查詢的 "結束日期"。
    5. 如果只提到 "今天"、"昨天" 或 "10/20"，則 start_date 和 end_date 應為同一天。
    6. 如果提到 "這禮拜" 或 "本週"，使用 {start_of_week.strftime('%Y-%m-%d')} 到 {today_str}。
    7. 如果提到 "上禮拜" 或 "上週"，使用 {start_of_last_week.strftime('%Y-%m-%d')} 到 {end_of_last_week.strftime('%Y-%m-%d')}。
    8. 如果提到 "這個月" 或 "本月"，使用 {start_of_month.strftime('%Y-%m-%d')} 到 {today_str}。
    9. 如果提到 "上個月"，使用 {start_of_last_month.strftime('%Y-%m-%d')} 到 {last_month_end_date.strftime('%Y-%m-%d')}。
    10. (重要) 如果關鍵字包含乘法 (例如 "龜甲萬豆乳紅茶")，請確保 keyword 欄位是精確的 (例如 "龜甲萬豆乳紅茶")。

    範例：

    ⚠️ 規則補充：
    - 如果使用者輸入金額中有「+」或「x/＊」符號（例如 "晚餐180+60+135"、"飲料59x2"），
      請將它們視為「單一筆記帳」的運算表達式，**計算總和**後輸出一筆金額，而不是拆成多筆。
      例如：
      輸入: "晚餐180+60+135" -> {"status": "success", "data": [{"datetime": "{today_str} 18:00:00", "category": "餐飲", "amount": -375, "notes": "晚餐"}], "message": "記錄成功"}
      輸入: "飲料59x2" -> {"status": "success", "data": [{"datetime": "{today_str} 12:00:00", "category": "飲料", "amount": -118, "notes": "飲料"}], "message": "記錄成功"}
    輸入: "雞排" -> {{"status": "success", "keyword": "雞排", "start_date": null, "end_date": null, "message": "查詢關鍵字：雞排"}}
    輸入: "這禮拜的餐飲" -> {{"status": "success", "keyword": "餐飲", "start_date": "{start_of_week.strftime('%Y-%m-%d')}", "end_date": "{today_str}", "message": "查詢本週的餐飲"}}
    輸入: "幫我查上禮拜飲料花多少" -> {{"status": "success", "keyword": "飲料", "start_date": "{start_of_last_week.strftime('%Y-%m-%d')}", "end_date": "{end_of_last_week.strftime('%Y-%m-%d')}", "message": "查詢上禮拜的飲料"}}
    輸入: "上個月" -> {{"status": "success", "keyword": null, "start_date": "{start_of_last_month.strftime('%Y-%m-%d')}", "end_date": "{last_month_end_date.strftime('%Y-%m-%d')}", "message": "查詢上個月的記錄"}}
    輸入: "10/1 到 10/10" -> {{"status": "success", "keyword": null, "start_date": "{today.year}-10-01", "end_date": "{today.year}-10-10", "message": "查詢 10/ 到 10/10"}}
    輸入: "昨天" -> {{"status": "success", "keyword": null, "start_date": "{(today - timedelta(days=1)).strftime('%Y-%m-%d')}", "end_date": "{(today - timedelta(days=1)).strftime('%Y-%m-%d')}", "message": "查詢昨天的記錄"}}
    輸入: "龜甲萬豆乳紅茶" -> {{"status": "success", "keyword": "龜甲萬豆乳紅茶", "start_date": null, "end_date": null, "message": "查詢關鍵字：龜甲萬豆乳紅茶"}}
    """

    try:
        logger.debug("發送 search prompt 至 Gemini API")
        response = gemini_model.generate_content(prompt)
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        logger.debug(f"Gemini Search response: {clean_response}")
        
        parsed_query = json.loads(clean_response)
        return parsed_query
        
    except json.JSONDecodeError as e:
        logger.error(f"Gemini Search JSON 解析失敗: {clean_response}")
        return {"status": "failure", "message": f"AI 分析器 JSON 解析失敗: {e}"}
    except Exception as e:
        logger.error(f"Gemini Search API 呼叫失敗: {e}", exc_info=True)
        return {"status": "failure", "message": f"AI 分析器 API 呼叫失敗: {e}"}

# === 主程式入口 ===
if __name__ == "__main__":
    logger.info("Starting Flask server locally...")
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)