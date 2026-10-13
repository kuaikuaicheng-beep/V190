import os, re, sqlite3, asyncio, json, io, traceback, random, zipfile, hashlib
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands
from discord.ext import commands, tasks


TOKEN = os.getenv('DISCORD_TOKEN','').strip()
DB_PATH = os.getenv('DB_PATH','/data/trade_bot.db' if os.path.isdir('/data') else 'trade_bot.db')

COIN_PRODUCTS=['100M（普通）','100M（不死）']
BULLET_SERVICE='A大子彈'      # 服務名稱（orders.service_type）
BULLET_PRODUCT='A大子彈'      # 商品名稱（products.product），單位是「發」
SELLABLE_PRODUCTS=COIN_PRODUCTS+[BULLET_PRODUCT]
LOGIN_TYPES=['Garena登入','Google登入']
BOOST_TIERS=[f'{x}M' for x in [50,100,150,200,300,400,500,600,700,800,900,1000]]
# /查詢工單 的商品 choices 必須在 decorator 載入前存在。
QUERY_PRODUCT_CHOICES = SELLABLE_PRODUCTS + BOOST_TIERS
STATUSES=['結單','待付款','待確認付款','待洽談','待排單','待交貨','處理中','待收貨','有爭議','待處理','已取消']
AVAILABILITY=['正常提供','暫停提供','缺貨']
# 只把「看起來像工單」的頻道視為新工單，避免一般數字頻道被誤判。
TICKET_STATUSES = STATUSES
RENAME_ONLY_STATUSES = ('待倒',)
TICKET_NAME_STATUSES = tuple(dict.fromkeys((*TICKET_STATUSES, *RENAME_ONLY_STATUSES)))
TICKET_STATUS_PATTERN = '|'.join(re.escape(x) for x in TICKET_NAME_STATUSES)
CLOSED_MARKERS=('結單','已結單','已關閉','closed','close','archived','archive')

db=sqlite3.connect(DB_PATH,check_same_thread=False,timeout=15)
db.row_factory=sqlite3.Row
db.execute('PRAGMA journal_mode=WAL')
db.execute('PRAGMA synchronous=NORMAL')
db.execute('PRAGMA busy_timeout=15000')
db.execute('PRAGMA foreign_keys=ON')
lock=asyncio.Lock()

def q(sql,params=(),fetch=False):
    # SELECT / PRAGMA 不需要 commit，避免每一則訊息都產生不必要的磁碟 I/O。
    c=db.cursor(); c.execute(sql,params); rows=c.fetchall() if fetch else None
    first=(sql.lstrip().split(None,1)[0].upper() if sql.lstrip() else '')
    if first not in ('SELECT','PRAGMA','EXPLAIN'):
        db.commit()
    return rows

def now(): return datetime.now(timezone.utc).isoformat()

def key(guild_id,k): return f'g:{guild_id}:{k}'
# 設定值快取：設定很少變、卻幾乎每個指令／按鈕都會讀好幾次；寫入（setv／匯入設定）時同步更新，所以不會讀到舊值。
_SETTINGS_CACHE = {}
def get(guild_id,k,default=''):
    kk=key(guild_id,k)
    if kk not in _SETTINGS_CACHE:
        r=q('SELECT value FROM settings WHERE key=?',(kk,),True)
        _SETTINGS_CACHE[kk]=r[0]['value'] if r else None   # None＝資料庫沒有這個設定（也快取，省下重複查詢）
    v=_SETTINGS_CACHE[kk]
    return default if v is None else v
def setv(guild_id,k,v):
    q('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key(guild_id,k),str(v)))
    _SETTINGS_CACHE[key(guild_id,k)]=str(v)

def choices(vals): return [app_commands.Choice(name=x,value=x) for x in vals]

def safe_format(text, **kw):
    """只替換 {代號}（代號必須是傳入的名稱），其他大括號原樣保留。
    店長自訂訊息若含多餘的 { } 或打錯代號，用 str.format 會直接丟例外、讓客人的付款頁當掉；這個版本不會。"""
    text=str(text)
    return re.sub(r'\{(\w+)\}',lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0),text)

# ── 介面樣式：統一色票、狀態圖示與 Embed 小工具 ──────────────────────────
UI_COLOR = {'brand': 0x5865F2, 'ok': 0x2ECC71, 'warn': 0xF1C40F, 'danger': 0xE74C3C,
            'info': 0x3498DB, 'money': 0xF39C12, 'muted': 0x95A5A6}
STATUS_STYLE = {'待付款': ('🟡', 'warn'), '待確認付款': ('🟠', 'money'), '待洽談': ('💬', 'info'), '待排單': ('🗂️', 'info'),
                '待交貨': ('📦', 'info'), '處理中': ('🔧', 'brand'), '待收貨': ('📬', 'money'), '有爭議': ('⚠️', 'danger'),
                '待處理': ('🛠️', 'info'), '結單': ('✅', 'ok'), '已取消': ('⚫', 'muted')}
def status_icon(st): return STATUS_STYLE.get(str(st), ('⚪', 'brand'))[0]
def status_color(st): return UI_COLOR[STATUS_STYLE.get(str(st), ('⚪', 'brand'))[1]]
def clip(text, n=1000):
    """Embed 欄位長度上限保護（欄位 1024、描述 4096），超過就截斷並加上省略號。"""
    text = str(text); return text if len(text) <= n else text[:n - 1] + '…'
def brand_footer(e, extra=''):
    e.set_footer(text='CT小舖' + (f'｜{extra}' if extra else '')); return e

def init_db():
    q('''CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_no TEXT NOT NULL,channel_id TEXT NOT NULL,guild_id TEXT NOT NULL,product TEXT NOT NULL,quantity INTEGER NOT NULL,unit_price INTEGER NOT NULL,total_price INTEGER NOT NULL,status TEXT NOT NULL DEFAULT '待付款',buyer_id TEXT,created_at TEXT NOT NULL,paid_at TEXT,completed_at TEXT,delivery_time TEXT,payment_method TEXT,payment_reminder_sent INTEGER NOT NULL DEFAULT 0)''')
    # 舊版資料庫相容：補上新流程需要的欄位。
    cols={r['name'] for r in q('PRAGMA table_info(orders)',(),True)}
    if 'delivery_time' not in cols: q('ALTER TABLE orders ADD COLUMN delivery_time TEXT')
    if 'payment_method' not in cols: q('ALTER TABLE orders ADD COLUMN payment_method TEXT')
    if 'payment_reminder_sent' not in cols: q('ALTER TABLE orders ADD COLUMN payment_reminder_sent INTEGER NOT NULL DEFAULT 0')
    if 'service_type' not in cols: q("ALTER TABLE orders ADD COLUMN service_type TEXT NOT NULL DEFAULT '幣號'")
    if 'game_account' not in cols: q('ALTER TABLE orders ADD COLUMN game_account TEXT')
    if 'payment_proof_url' not in cols: q('ALTER TABLE orders ADD COLUMN payment_proof_url TEXT')
    if 'payment_proof_message_id' not in cols: q('ALTER TABLE orders ADD COLUMN payment_proof_message_id TEXT')
    if 'stock_reserved' not in cols: q('ALTER TABLE orders ADD COLUMN stock_reserved INTEGER NOT NULL DEFAULT 0')
    # 餘額付款：balance_paid＝這張單用餘額付了多少；balance_refunded＝取消時是否已退回（旗標防止重複退）。
    if 'balance_paid' not in cols: q('ALTER TABLE orders ADD COLUMN balance_paid INTEGER NOT NULL DEFAULT 0')
    if 'balance_refunded' not in cols: q('ALTER TABLE orders ADD COLUMN balance_refunded INTEGER NOT NULL DEFAULT 0')
    q('''CREATE TABLE IF NOT EXISTS products(product TEXT PRIMARY KEY,price INTEGER NOT NULL DEFAULT 0,stock INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,availability_status TEXT NOT NULL DEFAULT '正常提供')''')
    pcols={r['name'] for r in q('PRAGMA table_info(products)',(),True)}
    if 'availability_status' not in pcols: q("ALTER TABLE products ADD COLUMN availability_status TEXT NOT NULL DEFAULT '正常提供'")
    q('''CREATE TABLE IF NOT EXISTS boost_prices(guild_id TEXT NOT NULL,tier TEXT NOT NULL,price INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(guild_id,tier))''')
    q('''CREATE TABLE IF NOT EXISTS ticket_channels(channel_id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,ticket_no TEXT NOT NULL,first_name TEXT NOT NULL,buyer_id TEXT,buyer_name TEXT,detected_at TEXT NOT NULL)''')
    q('''CREATE TABLE IF NOT EXISTS ticket_counters(guild_id TEXT PRIMARY KEY,next_no INTEGER NOT NULL DEFAULT 1)''')
    q('''CREATE TABLE IF NOT EXISTS balances(user_id TEXT PRIMARY KEY,balance INTEGER NOT NULL DEFAULT 0)''')
    q('''CREATE TABLE IF NOT EXISTS balance_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id TEXT NOT NULL,user_id TEXT NOT NULL,operator_id TEXT NOT NULL,amount INTEGER NOT NULL,balance_after INTEGER NOT NULL,action TEXT NOT NULL,created_at TEXT NOT NULL,note TEXT)''')
    q('''CREATE TABLE IF NOT EXISTS order_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL,guild_id TEXT NOT NULL,operator_id TEXT,action TEXT NOT NULL,detail TEXT,created_at TEXT NOT NULL)''')
    q('''CREATE TABLE IF NOT EXISTS progress_posts(order_id INTEGER PRIMARY KEY,channel_id TEXT NOT NULL,message_id TEXT NOT NULL,status TEXT NOT NULL)''')
    q('''CREATE TABLE IF NOT EXISTS status_posts(order_id INTEGER PRIMARY KEY,channel_id TEXT NOT NULL,message_id TEXT NOT NULL,updated_at TEXT NOT NULL)''')
    q('''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '')''')
    q('''CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id TEXT NOT NULL,product TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'available',order_id INTEGER,channel_id TEXT,issued_by TEXT,issued_at TEXT,created_at TEXT NOT NULL)''')
    _acols=[r['name'] for r in q('PRAGMA table_info(accounts)',(),True)]
    if 'login_type' not in _acols: q("ALTER TABLE accounts ADD COLUMN login_type TEXT NOT NULL DEFAULT ''")
    q('''CREATE TABLE IF NOT EXISTS giveaways(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id TEXT NOT NULL,channel_id TEXT NOT NULL,message_id TEXT NOT NULL DEFAULT '',prize TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',winners INTEGER NOT NULL DEFAULT 1,host_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',winner_ids TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,ended_at TEXT)''')
    q('''CREATE TABLE IF NOT EXISTS giveaway_entries(giveaway_id INTEGER NOT NULL,user_id TEXT NOT NULL,joined_at TEXT NOT NULL,PRIMARY KEY(giveaway_id,user_id))''')
    q('CREATE INDEX IF NOT EXISTS idx_giveaways_msg ON giveaways(message_id)')
    q('''CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id TEXT NOT NULL,user_id TEXT NOT NULL,rating INTEGER NOT NULL DEFAULT 0,answers TEXT NOT NULL DEFAULT '[]',channel_id TEXT NOT NULL DEFAULT '',message_id TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)''')
    _rcols={r['name'] for r in q('PRAGMA table_info(reviews)',(),True)}
    for _c in ('product','comment','panel_id'):
        if _c not in _rcols: q(f"ALTER TABLE reviews ADD COLUMN {_c} TEXT NOT NULL DEFAULT ''")
    q('CREATE INDEX IF NOT EXISTS idx_reviews_guild ON reviews(guild_id,created_at)')
    q('''CREATE TABLE IF NOT EXISTS account_msg_log(message_id INTEGER NOT NULL,idx INTEGER NOT NULL,guild_id TEXT NOT NULL,channel_id TEXT NOT NULL,ticket_no TEXT NOT NULL DEFAULT '',author_id TEXT NOT NULL DEFAULT '',fingerprint TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'mail',shown TEXT NOT NULL DEFAULT '',sent_at TEXT NOT NULL DEFAULT '',warned INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(message_id,idx))''')
    q('CREATE INDEX IF NOT EXISTS idx_account_msg_fp ON account_msg_log(guild_id,fingerprint)')
    q('''CREATE TABLE IF NOT EXISTS account_scan_state(channel_id TEXT PRIMARY KEY,last_message_id TEXT NOT NULL)''')
    q('CREATE INDEX IF NOT EXISTS idx_accounts_pick ON accounts(guild_id,product,status,id)')
    q('CREATE INDEX IF NOT EXISTS idx_accounts_order ON accounts(order_id)')
    q('CREATE INDEX IF NOT EXISTS idx_orders_channel_buyer_status ON orders(channel_id,buyer_id,status)')
    q('CREATE INDEX IF NOT EXISTS idx_orders_guild_status_reminder ON orders(guild_id,status,payment_reminder_sent)')
    q('CREATE INDEX IF NOT EXISTS idx_orders_guild_created ON orders(guild_id,created_at)')
    q('CREATE INDEX IF NOT EXISTS idx_ticket_channels_guild_buyer ON ticket_channels(guild_id,buyer_id)')
    q('CREATE INDEX IF NOT EXISTS idx_ticket_channels_guild_no ON ticket_channels(guild_id,ticket_no)')
init_db()
for _p in SELLABLE_PRODUCTS:
    q('INSERT OR IGNORE INTO products(product,price,stock,enabled,availability_status) VALUES(?,?,?,?,?)',(_p,0,0,1,'正常提供'))

intents=discord.Intents.default(); intents.guilds=True; intents.message_content=True
class CTCommandTree(app_commands.CommandTree):
    """所有 Slash Command 進來的第一道關卡：只能在伺服器內用，並依「權限分級表」檢查。
    表裡沒列的指令一律當成「管理員才能用」（預設拒絕），以後新增指令忘了加檢查也不會變成客人能用。"""
    async def interaction_check(self, interaction):
        data = getattr(interaction, 'data', None)
        name = data.get('name') if isinstance(data, dict) else None
        ok, msg = check_command_permission(interaction.user, name)
        if ok: return True
        try:        # 直接回覆拒絕訊息再回傳 False（不靠例外轉送，各版本 discord.py 行為都一樣）
            if not interaction.response.is_done(): await interaction.response.send_message(msg, ephemeral=True)
        except (discord.HTTPException, discord.NotFound): pass
        return False

bot=commands.Bot(command_prefix='!',intents=intents,tree_cls=CTCommandTree)
# 已知工單頻道快取：on_message 不再每一則普通聊天都查 SQLite。
TICKET_CHANNEL_IDS=set()
TICKET_SYNCED=False
STATUS_POST_LOCKS = {}
AUTO_CATEGORY_LOCKS = {}
RENUMBER_LOCK = asyncio.Lock()
AUTO_CATEGORY_MODES=['工單號小-大','工單號大-小','按照目前排序狀況','隨機']
# 防止同一位客人快速連點「開始開單」造成重複工單。
OPEN_TICKET_LOCKS = {}
VIEWS_REGISTERED = False
DETECTION_LOCKS = {}

_ADMIN_ROLE_NAMES = {'管理員', '店長', '店長👑'}   # 注意：「闆闆」是客人身分組，絕對不能放進來

def admin_reasons(m):
    """這個成員為什麼算管理員？回傳 [(級別, 原因)]；空列表＝不是管理員。
    級別 'strict'＝連金錢／資料類指令（加扣餘額、匯出匯入）也能用；'normal'＝只能用一般管理指令（客服身分組）。
    身分組比對一律排除 @everyone（它的 ID 等於伺服器 ID）——否則有人誤把 @everyone 設成管理員／客服身分組，全部客人都會變管理員。"""
    out = []
    if not isinstance(m, discord.Member): return out
    try:
        gid = str(m.guild.id)
        perms = m.guild_permissions
        if perms.administrator: out.append(('strict', 'Discord「管理員」權限'))
        elif perms.manage_guild: out.append(('strict', 'Discord「管理伺服器」權限'))
        # 只認「管理員」「管理伺服器」；「管理頻道」是版主／客服常見權限，不等於店內管理員。
        rid = str(get(m.guild.id, 'admin_role_id', '') or '')
        sid = str(get(m.guild.id, 'support_role_id', '') or '')
        for r in m.roles:
            if str(r.id) == gid: continue
            if rid and str(r.id) == rid: out.append(('strict', f'有管理員身分組「{r.name}」（/設定管理員）'))
            elif sid and str(r.id) == sid: out.append(('normal', f'有客服身分組「{r.name}」（/設定客服身分組，視同管理員）'))
            elif (r.name or '').strip() in _ADMIN_ROLE_NAMES: out.append(('strict', f'身分組名稱「{r.name}」符合內建管理員名稱'))
    except Exception as e:
        print(f'[ADMIN_CHECK] guild={getattr(getattr(m, "guild", None), "id", None)} user={getattr(m, "id", None)} error={e!r}')
        return []
    return out

def admin(m):
    """統一判斷 CT 小舖管理員（Discord 管理權限、管理員身分組、客服身分組、內建管理員名稱）。"""
    return bool(admin_reasons(m))

def strict_admin(m):
    """金錢與資料類指令專用（加扣餘額、匯出／匯入設定、匯入資料庫）。客服身分組不算。"""
    return any(level == 'strict' for level, _ in admin_reasons(m))

# ===== 權限分級（單一來源）=====
# public：任何成員都能用（指令內部仍會再限制「只能看自己的」）。
# strict ：店長等級（Discord 管理員／管理伺服器／管理員身分組／內建店長名稱）；客服身分組不行。
# 其餘（表裡沒列）：管理員等級（含客服身分組）。新增指令忘了列，預設就是管理員才能用。
PUBLIC_COMMANDS = frozenset({'查詢餘額', '餘額紀錄', '購買相關資訊'})
STRICT_COMMANDS = frozenset({
    # 金錢
    '增加餘額', '扣除餘額', '餘額總覽', '設定價格', '設定代肝價格',
    # 資料（含帳號明文）
    '匯出資料庫', '匯入資料庫', '匯出設定', '匯入設定', '帳號庫存總表',
    # 誰有權限、付款資訊、稽核紀錄頻道（改這些可以提權、轉走客人的錢、或讓操作不留紀錄）
    '設定管理員', '設定客服身分組', '設定開單身分組', '設定付款', '設定付款方式',
    '設定操作紀錄', '設定訂單紀錄', '設定狀態頻道',
    # 一次影響很多張工單
    '關所有工單', '關閉結單工單',
})

def command_level(root_name):
    """指令（頂層名稱）要求的等級：'public'／'strict'／'admin'。名稱不明（例如右鍵選單）一律 'admin'。"""
    name = str(root_name or '')
    if name in STRICT_COMMANDS: return 'strict'
    if name in PUBLIC_COMMANDS: return 'public'
    return 'admin'

def check_command_permission(member, root_name):
    """統一閘門的判斷：回傳 (通過與否, 拒絕訊息)。"""
    if not isinstance(member, discord.Member):
        return False, '❌ 這個指令只能在伺服器內使用。'
    level = command_level(root_name)
    if level == 'public': return True, ''
    if level == 'strict':
        if strict_admin(member): return True, ''
        return False, STRICT_DENY_TEXT
    if admin(member): return True, ''
    return False, ADMIN_DENY_TEXT

ADMIN_DENY_TEXT = '❌ 你沒有管理權限。\n需要 Discord「管理員」或「管理伺服器」權限，或被設定為 CT小舖 管理員／客服身分組。（可用 `/檢查權限` 查原因）'
STRICT_DENY_TEXT = '❌ 這個指令只有**店長等級**可以用（Discord「管理員」或「管理伺服器」權限、管理員身分組），客服身分組不行。'

def permission_report(m, guild):
    """給 /檢查權限 用：這個成員在機器人眼中是什麼身分、為什麼，以及目前的管理員／客服身分組設定有沒有問題。"""
    reasons = admin_reasons(m)
    lines = [f'**{m.mention}**：' + ('✅ 算管理員（可用一般管理指令）' if reasons else '❌ **不是管理員**（只能用客人指令）')]
    if reasons: lines.append('　店長級指令（加扣餘額、匯出匯入）：' + ('✅ 可以' if any(l == 'strict' for l, _ in reasons) else '❌ 不行'))
    lines += [f'• {why}' for _l, why in reasons]
    gid = str(guild.id)
    for key, label in (('admin_role_id', '管理員身分組'), ('support_role_id', '客服身分組')):
        rid = str(get(guild.id, key, '') or '')
        if not rid: lines.append(f'{label}：（沒設定）'); continue
        role = guild.get_role(int(rid)) if rid.isdigit() else None
        if rid == gid: lines.append(f'{label}：⚠️ 設成了 @everyone（機器人已忽略它；請改設正確的身分組）')
        elif role is None: lines.append(f'{label}：⚠️ 找不到這個身分組（可能已被刪除），請重新設定')
        else: lines.append(f'{label}：{role.mention}')
    return '\n'.join(lines)

def ticket_no(name):
    """嚴格辨識工單號。

    規則優先順序：
    1. 明確的「狀態-服務-工單號-額度」格式
    2. 舊版「狀態-工單號-額度/數量」格式
    3. closed/close/archived + 工單號
    4. ticket/工單 + 工單號
    5. 最後才接受「狀態/服務 + 分隔符 + 數字」格式

    重點：不會把 50M、100M、200M 或數量誤認成工單號。
    """
    name = (name or '').strip()
    if not name:
        return None

    # Discord 頻道名稱會把部分字元正規化；統一比較用的分隔符。
    n = re.sub(r'\s+', ' ', name)

    # ① 現行改名工單格式：
    #    待倒-代肝-0027-100m
    #    結單-幣號-0042-50m
    #    結單-幣號-0178-100m1隻
    # 工單號「必須」位於服務名稱後面的獨立數字段。
    m = re.fullmatch(
        rf'(?:{TICKET_STATUS_PATTERN})[-_ ]+(?:幣號|代肝|子彈)[-_ ]+(\d{{1,8}})(?:[-_ ].*)?',
        n, re.I
    )
    if m:
        return m.group(1)

    # ② ticket + 工單號：
    #    ticket-0018-c'
    #    ticket-0018
    #    ticket-幣號-0018-100m
    #    ticket_support_0018-extra
    m = re.match(r'(?i)^ticket(?:[-_ ]+(?:幣號|代肝|子彈))?[-_ ]+(\d{1,8})(?:[-_ ].*)?$', n)
    if m:
        return m.group(1)

    # ticket 後面有額外文字時，仍只抓「ticket 後第一個合理數字」，
    # 但禁止從額度 50M/100M 取號。
    if re.match(r'(?i)^ticket(?:[-_ ]|$)', n):
        nums = list(re.finditer(r'(?<!\d)(\d{1,8})(?!\d)', n))
        for x in nums:
            pos = x.start(1)
            after = n[pos + len(x.group(1)):pos + len(x.group(1)) + 2]
            if not re.match(r'(?i)M', after):
                return x.group(1)

    # ③ closed / close / archived：
    #    closed-0042
    #    closed-幣號-0042-50m
    m = re.match(
        r'(?i)^(?:closed|close|archived|archive)(?:[-_ ]+(?:幣號|代肝|子彈))?[-_ ]+(\d{1,8})(?:[-_ ].*)?$',
        n
    )
    if m:
        return m.group(1)

    # ④ 「工單」關鍵字：
    #    工單-0018
    #    工單-幣號-0018-100m
    #    工單 #0018
    m = re.match(rf'^(?:工單)(?:[-_ ]+(?:幣號|代肝|子彈))?[-_ #：:]+(\d{{1,8}})(?:[-_ ].*)?$', n, re.I)
    if m:
        return m.group(1)

    if '工單' in n:
        nums = list(re.finditer(r'(?<!\d)(\d{1,8})(?!\d)', n))
        for x in nums:
            pos = x.start(1)
            before = n[max(0, pos - 3):pos]
            after = n[pos + len(x.group(1)):pos + len(x.group(1)) + 3]
            # 額度／數量組合，例如「工單-50M2隻」：50 是額度、2 是數量，兩者都不能當工單號。
            if re.search(r'(?i)m\s*$', before) or re.match(r'(?i)^m', after):
                continue
            if re.search(r'(?i)\d+\s*m\s*$', before) and re.match(r'\s*隻', after):
                continue
            if '#' in before or re.match(r'(?i)^[-_ ]', before[-1:] or ' ') or not re.match(r'(?i)m', after):
                # 數字若直接是「數量＋隻」且前方已有 M，排除。
                if re.search(r'(?i)m\s*$', before):
                    continue
                return x.group(1)

    # ⑤ 舊版：狀態-工單號-額度/數量
    #    結單-0042-50m
    #    結單-0178-100m1隻
    m = re.fullmatch(
        rf'(?:{TICKET_STATUS_PATTERN})[-_ ]+(\d{{1,8}})[-_ ]+.*',
        n, re.I
    )
    if m:
        return m.group(1)

    # ⑥ 最後保底：狀態/服務 + 分隔符 + 數字。
    # 只接受「狀態或服務後面第一個獨立數字段」，不掃整串找最後一個數字。
    if n.startswith(tuple(TICKET_STATUSES)) or '幣號' in n or '代肝' in n or '子彈' in n:
        m = re.search(r'(?:' + TICKET_STATUS_PATTERN + r'|幣號|代肝|子彈)[-_ ]+(\d{1,8})(?!\d)', n, re.I)
        if m:
            candidate = m.group(1)
            after = n[m.end(1):m.end(1)+2]
            # 若這個數字直接是 50M/100M 等額度，不能當工單號。
            if not re.match(r'(?i)M', after):
                return candidate

    return None

def looks_closed(name):
    n=(name or '').lower()
    return any(marker.lower() in n for marker in CLOSED_MARKERS)

def is_ticket_candidate(ch):
    if not isinstance(ch,discord.TextChannel): return False
    name=(ch.name or '').strip()
    if q('SELECT 1 FROM ticket_channels WHERE channel_id=? LIMIT 1',(str(ch.id),),True): return True
    if q('SELECT 1 FROM orders WHERE channel_id=? LIMIT 1',(str(ch.id),),True): return True
    # 新建外部工單、已改名工單、舊版狀態工單都可辨識；一般頻道必須有明確工單語意。
    if bool(ticket_no(name)) and (
        bool(re.match(r'(?i)^ticket', name)) or '工單' in name or
        bool(re.match(r'(?i)^(closed|close|archived|archive)[-_ ]', name)) or
        any(name.startswith(st) for st in TICKET_STATUSES) or '幣號' in name or '代肝' in name or '子彈' in name
    ):
        return True
    return _loose_ticket_no(ch) is not None

def price(p):
    r=q('SELECT price FROM products WHERE product=? AND enabled=1',(p,),True)
    if not r: return 0
    v=float(r[0]['price'] or 0)
    return int(v) if v==int(v) else v

def fmt_price(v):
    """單價顯示：整數不帶小數，其餘最多 4 位小數（去掉尾端 0）。"""
    v=float(v or 0)
    if v==int(v): return f'{int(v):,}'
    return f'{v:,.4f}'.rstrip('0').rstrip('.')

def calc_total(unit,qty):
    """總價 = 單價 × 數量，四捨五入到整數 NT（付款一律整數元）。"""
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(str(unit))*Decimal(int(qty))).quantize(Decimal('1'),rounding=ROUND_HALF_UP))

def stock(p):
    r=q('SELECT stock FROM products WHERE product=?',(p,),True); return int(r[0]['stock']) if r else 0

def release_reserved_stock(oid,old_status,new_status):
    """訂單狀態被「手動」改掉時，把預扣的庫存退回。

    - 改成「已取消」：退回（已進入「待收貨」或「結單」代表已交付，不退）。
    - 從「待付款」直接改成「結單」：這張單從沒付款，也要退回。
    以 stock_reserved 旗標控管，先清旗標再加庫存，重複呼叫不會重複退。回傳是否有退。
    """
    old_status=str(old_status or ''); new_status=str(new_status or '')
    if not ((new_status=='已取消' and old_status not in ('待收貨','結單')) or (new_status=='結單' and old_status=='待付款')):
        return False
    with db:
        cur=db.cursor()
        cur.execute('SELECT product,quantity FROM orders WHERE id=? AND stock_reserved=1',(oid,))
        row=cur.fetchone()
        if not row: return False
        cur.execute('UPDATE orders SET stock_reserved=0 WHERE id=? AND stock_reserved=1',(oid,))
        if cur.rowcount!=1: return False
        cur.execute('UPDATE products SET stock=stock+? WHERE product=?',(int(row['quantity']),row['product']))
    return True

def pay_order_with_balance(oid,user_id):
    """用餘額付清一張「待付款」訂單。回傳 (成功與否, 訊息, 訂單)。

    讀餘額、扣款、寫流水、改訂單狀態都在同一個 transaction 內（全程沒有 await，事件迴圈不會插隊），
    且 UPDATE 帶 status='待付款' 條件，所以連點或同時按兩次也只會扣一次。
    餘額不足不會部分扣款。付清後直接進「待交貨」（代肝進「待排單」），不需要客人上傳付款明細。
    """
    uid=str(user_id)
    with db:
        cur=db.cursor()
        cur.execute('SELECT * FROM orders WHERE id=?',(oid,)); o=cur.fetchone()
        if not o: return False,'❌ 找不到訂單。',None
        if str(o['buyer_id'])!=uid: return False,'❌ 這不是您的訂單，無法操作。',o
        if o['status']!='待付款': return False,f'ℹ️ 這筆訂單目前是「{o["status"]}」，不能再用餘額付款。',o
        total=int(o['total_price'] or 0)
        if total<=0: return False,'❌ 訂單金額異常，請聯絡店長。',o
        cur.execute('SELECT balance FROM balances WHERE user_id=?',(uid,)); r=cur.fetchone()
        bal=int(r['balance']) if r else 0
        if bal<total: return False,f'❌ 餘額不足：目前 NT${bal:,}，本單需要 NT${total:,}（還差 NT${total-bal:,}）。',o
        new_bal=bal-total
        nxt='待排單' if o['service_type']=='代肝' else '待交貨'
        cur.execute("UPDATE orders SET status=?,paid_at=?,payment_method='餘額',balance_paid=?,balance_refunded=0 WHERE id=? AND status='待付款'",(nxt,now(),total,oid))
        if cur.rowcount!=1: return False,'ℹ️ 這筆訂單狀態剛剛已變動，請重新確認。',o
        cur.execute('UPDATE balances SET balance=? WHERE user_id=?',(new_bal,uid))
        cur.execute('INSERT INTO balance_logs(guild_id,user_id,operator_id,amount,balance_after,action,created_at,note) VALUES(?,?,?,?,?,?,?,?)',(str(o['guild_id']),uid,uid,total,new_bal,'訂單付款',now(),f'訂單 #{o["ticket_no"]}｜{o["product"]} × {o["quantity"]}'))
    row=q('SELECT * FROM orders WHERE id=?',(oid,),True)[0]
    return True,f'✅ 已用餘額付款 NT${total:,}，餘額剩 NT${new_bal:,}。',row

def refund_balance_on_cancel(oid,old_status,new_status,operator_id=0):
    """訂單被改成「已取消」時，把當初用餘額付的錢退回餘額。回傳退回金額（沒退回就是 0）。

    條件與庫存退回一致：已進入「待收貨」或「結單」代表已交付，不自動退（需店長手動處理）。
    以 balance_refunded 旗標控管，重複呼叫不會重複退。
    """
    if str(new_status)!='已取消' or str(old_status) in ('待收貨','結單'): return 0
    with db:
        cur=db.cursor()
        cur.execute('SELECT * FROM orders WHERE id=? AND balance_paid>0 AND balance_refunded=0',(oid,)); o=cur.fetchone()
        if not o: return 0
        cur.execute('UPDATE orders SET balance_refunded=1 WHERE id=? AND balance_refunded=0',(oid,))
        if cur.rowcount!=1: return 0
        amt=int(o['balance_paid']); uid=str(o['buyer_id'])
        cur.execute('SELECT balance FROM balances WHERE user_id=?',(uid,)); r=cur.fetchone()
        new_bal=(int(r['balance']) if r else 0)+amt
        cur.execute('INSERT INTO balances(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance',(uid,new_bal))
        cur.execute('INSERT INTO balance_logs(guild_id,user_id,operator_id,amount,balance_after,action,created_at,note) VALUES(?,?,?,?,?,?,?,?)',(str(o['guild_id']),uid,str(operator_id or 0),amt,new_bal,'訂單退款',now(),f'訂單 #{o["ticket_no"]} 已取消，餘額退回'))
    return amt

def balance_refund_pending(o, old_status):
    """這張訂單改成「已取消」時，會不會把餘額退回客人？（餘額付款、還沒退過、尚未交付）"""
    try:
        if int(o['balance_paid'] or 0)<=0 or int(o['balance_refunded'] or 0)!=0: return False
    except (KeyError,IndexError,TypeError,ValueError): return False
    return str(old_status) not in ('待收貨','結單')

def availability(p):
    r=q('SELECT availability_status FROM products WHERE product=?',(p,),True); return r[0]['availability_status'] if r else '正常提供'

def boost_price(gid,tier):
    r=q('SELECT price FROM boost_prices WHERE guild_id=? AND tier=?',(str(gid),tier),True); return int(r[0]['price']) if r else 0


def _calc_boost_unit(guild, amount):
    tier=f'{int(amount)}M'
    if tier not in BOOST_TIERS:
        return None
    price=boost_price(guild.id,tier)
    return price if price>0 else None


# 自動偵測價格，只認這幾種格式：
#   幣號：100M不死*x、100M普通*x（x＝數量，自己填；乘號可用 * × x，數量後面可加「隻」）
#   代肝：直接打額度，例如 200M、300m（不用數量；前後可加「代肝」兩個字），直接抓 /設定代肝價格 的價格
# 可用 + 、 , 空白 串多筆，例如 100M不死*2+200M。
# 其他任何訊息（包含 50*3、沒有 M 的數字、不是代肝額度的 M）一律不偵測。
AUTO_PRICE_SPECS={'不死':'100M（不死）','普通':'100M（普通）'}
_AUTO_COIN=r'100\s*[mMＭ]\s*(?:不死|普通)\s*(?:\*|＊|[xX×])\s*\d{1,5}\s*(?:隻|支|個)?'
_AUTO_BOOST=r'(?:代肝\s*)?\d{2,4}\s*[mMＭ](?!\s*(?:不死|普通))(?:\s*代肝)?'
_AUTO_ITEM=rf'(?:{_AUTO_COIN}|{_AUTO_BOOST})'
_AUTO_COIN_G=r'100\s*[mMＭ]\s*(不死|普通)\s*(?:\*|＊|[xX×])\s*(\d{1,5})\s*(?:隻|支|個)?'
_AUTO_BOOST_G=r'(?:代肝\s*)?(\d{2,4})\s*[mMＭ](?!\s*(?:不死|普通))(?:\s*代肝)?'

def parse_price_calculation(guild, content):
    text=(content or '').strip()
    if not text or len(text)>120:
        return None
    if not re.fullmatch(rf'\s*{_AUTO_ITEM}(?:\s*(?:\+|,|，|、|\s)\s*{_AUTO_ITEM})*\s*',text):
        return None
    total=0; details=[]; has_coin=False; has_boost=False
    for m in re.finditer(rf'{_AUTO_COIN_G}|{_AUTO_BOOST_G}',text):
        if m.group(1):   # 幣號
            has_coin=True
            qty=int(m.group(2))
            if qty<=0 or qty>10000:
                return {'type':'invalid','text':'❌ 數量請輸入 1～10000。'}
            product=AUTO_PRICE_SPECS[m.group(1)]
            unit=price(product)
            if unit<=0:
                return {'type':'missing','text':f'❌ 「{product}」尚未設定價格（請用 /設定價格）。'}
            subtotal=calc_total(unit,qty); total+=subtotal
            details.append(f'🪙 {product} × {qty} 隻 ＝ NT${subtotal:,}（單價 {fmt_price(unit)}）')
        else:            # 代肝
            amount=int(m.group(3))
            if f'{amount}M' not in BOOST_TIERS:
                return None   # 不是代肝額度（例如 75M）：當一般聊天，不回覆
            has_boost=True
            unit=_calc_boost_unit(guild,amount)
            if unit is None:
                return {'type':'missing','text':f'❌ 代肝 {amount}M 尚未設定價格（請用 /設定代肝價格）。'}
            total+=unit
            details.append(f'🛠️ 代肝 {amount}M ＝ NT${unit:,}')
    if not details:
        return None
    title='代肝' if has_boost and not has_coin else ('幣號' if has_coin and not has_boost else '')
    return {'type':'ok','text':f'💰 **{title}價格試算**\n'+'\n'.join(details)+f'\n\n**總計：NT${total:,}**'}


def channel_link(guild,name):
    setting='coin_price_channel_id' if name=='幣號價目表' else 'boost_price_channel_id'
    cid=get(guild.id,setting,'')
    ch=guild.get_channel(int(cid)) if cid.isdigit() else None
    if not isinstance(ch,discord.TextChannel): ch=discord.utils.get(guild.text_channels,name=name)
    return ch.mention if ch else f'#{name}'

def off_hours(gid):
    return get(gid,'off_hours','0')=='1'

def off_hours_back_line(gid):
    t=(get(gid,'off_hours_back','') or '').strip()
    return f'\n🕐 預計回覆時間：**{t}**' if t else ''

def ticket_record(ch):
    r=q('SELECT * FROM ticket_channels WHERE channel_id=?',(str(ch.id),),True); return r[0] if r else None

def remember(ch,no,buyer_id=None,buyer_name=None):
    TICKET_CHANNEL_IDS.add(str(ch.id))
    r=ticket_record(ch)
    if r:
        # 名稱／目前辨識結果優先；不要讓舊快取覆蓋剛整理出的正確工單號。
        updates=[]; params=[]
        if no is not None and str(no).isdigit() and str(r['ticket_no'] or '') != str(no):
            updates.append('ticket_no=?'); params.append(str(no))
        if buyer_id is not None and str(buyer_id).isdigit() and str(r['buyer_id'] or '') != str(buyer_id):
            updates.append('buyer_id=?'); params.append(str(buyer_id))
        if buyer_name and str(r['buyer_name'] or '') != str(buyer_name):
            updates.append('buyer_name=?'); params.append(str(buyer_name))
        if updates:
            params.append(str(ch.id)); q(f"UPDATE ticket_channels SET {', '.join(updates)} WHERE channel_id=?",tuple(params))
        return str(no) if no is not None else r['ticket_no']
    q('INSERT INTO ticket_channels(channel_id,guild_id,ticket_no,first_name,buyer_id,buyer_name,detected_at) VALUES(?,?,?,?,?,?,?)',(str(ch.id),str(ch.guild.id),str(no),ch.name,str(buyer_id) if buyer_id else None,buyer_name,now()))
    return no

def _is_staff_member(member):
    if not isinstance(member, discord.Member):
        return True
    if member.bot:
        return True
    if member.guild_permissions.administrator:
        return True
    # 以「管理員」名稱及已設定的管理員身分組雙重排除，避免把店長／管理員當成客人。
    admin_role_id = get(member.guild.id, 'admin_role_id', '')
    if admin_role_id and any(str(r.id) == str(admin_role_id) for r in member.roles):
        return True
    if any(r.name == '管理員' for r in member.roles):
        return True
    # 開單時 bot 會把「店長（ticket_owner_id；目前沒有指令可設定，通常是空的）」以個人權限加進每張工單，
    # 自動辨識客人時若不排除，店長就會被當成客人，別人的工單就變成店長的。
    owner_id = get(member.guild.id, 'ticket_owner_id', '')
    if owner_id and str(member.id) == str(owner_id):
        return True
    # 與權限系統一致：bot 自己認定的管理員（管理員／伺服器管理／店長角色名稱等）一律是店內人員。
    if admin(member):
        return True
    # 客服身分組也是店內人員：結單時不能把客服當成客人移除。
    support_id = get(member.guild.id, 'support_role_id', '')
    if support_id and any(str(r.id) == str(support_id) for r in member.roles):
        return True
    return False


def _member_can_view_channel(ch, member):
    try:
        return ch.permissions_for(member).view_channel
    except Exception:
        return False


async def detect_buyer(ch):
    """盡可能從「頻道本身」找出客人，而不是只依賴資料庫。

    判斷優先級：
    1. 頻道對「會員本人」的明確權限覆寫。
    2. 頻道實際可見的會員（在快取可用時）。
    3. 具有闆闆👑角色且可看頻道的會員。

    永遠排除 Bot、Administrator、管理員角色及設定的管理員角色。
    """
    candidates = []
    seen = set()

    # 第一層：最可靠——頻道對特定使用者的 Permission Overwrite。
    for target in await _overwrite_members(ch):
        overwrite = ch.overwrites_for(target)
        if _is_staff_member(target):
            continue
        # Ticket Bot 通常會直接給客人 view_channel=True。
        # 即使沒有明確寫 True，也用實際權限再確認一次，兼容不同 Ticket Bot 的關閉方式。
        explicit_view = overwrite.view_channel is True
        actual_view = _member_can_view_channel(ch, target)
        if explicit_view or actual_view:
            if target.id not in seen:
                candidates.append(target)
                seen.add(target.id)

    if candidates:
        # 有多個一般會員覆寫時取最早建立帳號（ID 最小）的那位，結果固定、不會因快取順序而變動。
        candidates.sort(key=lambda m: m.id)
        m = candidates[0]
        return m.id, m.display_name

    # 第二層：discord.py 已有成員快取時，直接看「這個頻道誰看得到」。
    # 不要求闆闆👑角色，因為關閉工單後 Ticket Bot 可能會改角色／權限。
    try:
        for member in getattr(ch, 'members', []):
            if _is_staff_member(member):
                continue
            if _member_can_view_channel(ch, member) and member.id not in seen:
                candidates.append(member)
                seen.add(member.id)
    except Exception:
        pass

    if candidates:
        candidates.sort(key=lambda m: m.id)
        m = candidates[0]
        return m.id, m.display_name

    # 第三層：兼容舊 Ticket Bot——如果頻道仍保留闆闆👑會員權限，就使用它。
    try:
        for target, overwrite in ch.overwrites.items():
            if not isinstance(target, discord.Member) or _is_staff_member(target):
                continue
            if not any(r.name == '闆闆👑' for r in target.roles):
                continue
            if overwrite.view_channel is not False and target.id not in seen:
                return target.id, target.display_name
    except Exception:
        pass

    # 第四層：關閉工單最可靠的備援——直接讀取頻道歷史訊息。
    # 很多 Ticket Bot 關單時會刪掉客人的 Permission Overwrite，因此「頻道裡誰有權限」
    # 會失效；但客人曾經在這張工單發過訊息，Discord 的訊息作者仍然存在。
    # 以最近 200 則訊息找「非 Bot、非管理員、非管理員角色」的真人，
    # 優先取最早出現的非管理人員，避免把店長後續回覆誤判成客人。
    try:
        history_candidates = []
        history_seen = set()
        async for msg in ch.history(limit=200, oldest_first=True):
            author = getattr(msg, 'author', None)
            if not isinstance(author, discord.Member) or _is_staff_member(author):
                continue
            if author.id in history_seen:
                continue
            history_seen.add(author.id)
            history_candidates.append(author)
        if history_candidates:
            # 取最早發言的非店內人員（客人通常是最先開口的人）。
            m = history_candidates[0]
            return m.id, m.display_name
    except Exception:
        pass

    return None, None

async def buyer_for(ch):
    """取得工單客人；已關閉工單仍盡量保留原客人。"""
    r = ticket_record(ch)

    # 1. 工單建立時已保存的 buyer_id 是第一優先。
    if r and r['buyer_id']:
        try:
            bid = int(r['buyer_id'])
        except (TypeError, ValueError):
            bid = None
        if bid:
            member = ch.guild.get_member(bid)
            bname = (member.display_name if member else r['buyer_name']) or '客人'
            if bname != (r['buyer_name'] or ''):
                q('UPDATE ticket_channels SET buyer_name=? WHERE channel_id=?', (bname, str(ch.id)))
            return bid, bname

    # 2. 從該工單最後一筆訂單記錄補回 buyer_id。
    rows = q('SELECT buyer_id FROM orders WHERE channel_id=? AND buyer_id IS NOT NULL ORDER BY id DESC LIMIT 1', (str(ch.id),), True)
    if rows and rows[0]['buyer_id']:
        try:
            bid = int(rows[0]['buyer_id'])
        except (TypeError, ValueError):
            bid = None
        if bid:
            member = ch.guild.get_member(bid)
            bname = member.display_name if member else (r['buyer_name'] if r else None)
            if r:
                q('UPDATE ticket_channels SET buyer_id=?,buyer_name=COALESCE(?,buyer_name) WHERE channel_id=?', (str(bid), bname, str(ch.id)))
            else:
                q('INSERT OR IGNORE INTO ticket_channels(channel_id,guild_id,ticket_no,first_name,buyer_id,buyer_name,detected_at) VALUES(?,?,?,?,?,?,?)', (str(ch.id), str(ch.guild.id), ticket_no(ch.name) or '0', ch.name, str(bid), bname, now()))
            return bid, bname

    # 3. 直接從「目前頻道」找客人。這是關閉工單最重要的備援。
    bid, bname = await detect_buyer(ch)
    if bid:
        if r:
            q('UPDATE ticket_channels SET buyer_id=?,buyer_name=? WHERE channel_id=?', (str(bid), bname, str(ch.id)))
        else:
            no = ticket_no(ch.name)
            if no:
                remember(ch, no, bid, bname)
        return bid, bname

    # 4. 如果曾經保存過名稱，即使現在找不到 Member，也保留原名稱，不回退成「客人」。
    if r and r['buyer_name']:
        return (int(r['buyer_id']) if r['buyer_id'] else None), r['buyer_name']

    return None, None


# === 結單時自動移除、重新開放時自動加回「闆闆」身分組成員 ===
BOSS_ROLE_NAME='闆闆'   # 身分組名稱（不含 @）；若想改用 ID，把 BOSS_ROLE_ID 設成數字
BOSS_ROLE_ID=None

def _find_boss_role(guild):
    if BOSS_ROLE_ID:
        r=guild.get_role(int(BOSS_ROLE_ID))
        if r: return r
    r=discord.utils.get(guild.roles,name=BOSS_ROLE_NAME)
    if r: return r
    for r in guild.roles:
        if BOSS_ROLE_NAME in r.name: return r
    return None


async def _overwrite_members(ch):
    """列出頻道「對會員本人」的權限覆寫，並把成員抓出來。

    沒開 members intent 時 guild 成員快取幾乎是空的，ch.overwrites 對沒快取的成員會回傳
    discord.Object（不是 Member），舊程式用 isinstance(..., Member) 全部略過，
    導致「找不到客人」「頻道內沒有闆闆成員」。這裡改成用 fetch_member 直接問 Discord。
    """
    out=[]
    for target in list(ch.overwrites.keys()):
        if isinstance(target, discord.Role):
            continue
        mem = target if isinstance(target, discord.Member) else ch.guild.get_member(target.id)
        if mem is None:
            try:
                mem = await ch.guild.fetch_member(target.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue   # 已離開伺服器或抓不到：他本來就看不到頻道
        out.append(mem)
    return out

def boss_role_can_see(ch):
    """「@闆闆👑」身分組本身在這個頻道（或所屬分類）是否看得到——有就代表整個身分組的人都看得到這張工單。"""
    try:
        role=_find_boss_role(ch.guild)
        if not role: return False
        v=ch.overwrites_for(role).view_channel
        if v is None:
            cat=getattr(ch,'category',None)
            if cat is not None: v=cat.overwrites_for(role).view_channel
        return v is True
    except Exception as e:
        print(f'[BOSS_ROLE] 檢查失敗｜channel={getattr(ch,"id","?")}｜error={e!r}'); return False

_DENY_KW=dict(view_channel=False,send_messages=False,read_message_history=False,attach_files=False,embed_links=False)

async def revoke_buyer_access(ch, buyer_id=None):
    """關單：移除客人 + 「闆闆」身分組成員的頻道存取權。"""
    me=ch.guild.me
    role=_find_boss_role(ch.guild)
    targets={}   # member_id -> Member

    # 1. 已知的客人 ID（資料庫／訂單／歷史訊息）
    try:
        if not buyer_id:
            buyer_id=(await buyer_for(ch))[0]
    except Exception as e:
        print(f'[CLOSE_PERMISSION] buyer_for 失敗｜channel={ch.id}｜error={e!r}')
        buyer_id=None
    if buyer_id and str(buyer_id).isdigit():
        m=ch.guild.get_member(int(buyer_id))
        if m is None:
            try: m=await ch.guild.fetch_member(int(buyer_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException): m=None
        if m: targets[m.id]=m

    # 2. 頻道上所有「會員個人覆寫」：Ticket Tool 等開單機器人都是用這種方式給客人權限
    try:
        for m in await _overwrite_members(ch):
            if m.bot or (me and m.id==me.id) or m.id in targets:
                continue
            is_boss = bool(role and role in m.roles)
            if ch.overwrites_for(m).view_channel is False:
                continue                       # 已經被拔過
            if _is_staff_member(m) and not is_boss:
                continue                       # 店長／管理員不動
            targets[m.id]=m
    except Exception as e:
        print(f'[CLOSE_PERMISSION] 讀取頻道覆寫失敗｜channel={ch.id}｜error={e!r}')

    role_msg=''
    if role and boss_role_can_see(ch):
        try:
            await asyncio.wait_for(ch.set_permissions(role, reason='CT小舖 關閉工單：移除「闆闆」身分組存取權限', **_DENY_KW), timeout=15)
            role_msg=f'已移除身分組「{role.name}」的存取'
        except (discord.Forbidden, discord.HTTPException, asyncio.TimeoutError) as e:
            role_msg=f'身分組「{role.name}」移除失敗'
            print(f'[CLOSE_PERMISSION] 闆闆身分組移除失敗｜channel={ch.id}｜error={e!r}')

    if not targets:
        if role_msg: return ('已移除' in role_msg), role_msg
        return False, '頻道內找不到任何客人／闆闆成員的個人權限覆寫（可能客人是靠身分組看到頻道）'

    removed=[]; failed=[]; admin_skip=[]
    for m in targets.values():
        if m.guild_permissions.administrator:
            admin_skip.append(m.display_name)  # 有 Administrator 時，頻道權限覆寫完全無效
            continue
        try:
            await asyncio.wait_for(ch.set_permissions(m, reason='CT小舖 關閉工單：移除客人／闆闆存取權限', **_DENY_KW), timeout=15)
            removed.append(m.display_name)
        except (discord.Forbidden, discord.HTTPException, asyncio.TimeoutError) as e:
            failed.append(m.display_name)
            print(f'[CLOSE_PERMISSION] 移除失敗｜channel={ch.id}｜member={m.id}｜error={e!r}')

    # 實際重新向 Discord 讀一次頻道，確認覆寫真的寫進去了（避免「看似成功、其實沒生效」）
    if removed:
        try:
            fresh=await ch.guild.fetch_channel(ch.id)
            still=[m for m in targets.values() if m.display_name in removed and fresh.overwrites_for(m).view_channel is not False]
            for m in still:
                removed.remove(m.display_name); failed.append(m.display_name)
        except Exception as e:
            print(f'[CLOSE_PERMISSION] 驗證失敗（略過）｜channel={ch.id}｜error={e!r}')

    parts=[]
    if removed: parts.append('已移除：'+'、'.join(removed))
    if failed: parts.append('移除失敗：'+'、'.join(failed)+'（請確認機器人有「管理身分組」且身分組高於對方）')
    if admin_skip: parts.append('無法移除（對方身分組有「管理員」權限，會無視頻道權限）：'+'、'.join(admin_skip))
    if role_msg: parts.append(role_msg)
    return bool(removed) or ('已移除' in role_msg), '｜'.join(parts)

async def restore_access(ch, buyer_id=None):
    """重新開放時把客人（及被一起移除的闆闆成員）加回來。
    只處理「有明確 view_channel=False 覆寫」的人，不會動到本來就能看的人。回傳 (恢復名單, 失敗名單)。"""
    me=ch.guild.me
    role=_find_boss_role(ch.guild)
    targets={}
    if buyer_id and str(buyer_id).isdigit():
        m=ch.guild.get_member(int(buyer_id))
        if m is None:
            try: m=await ch.guild.fetch_member(int(buyer_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException): m=None
        if m and ch.overwrites_for(m).view_channel is not True:
            targets[m.id]=m
    try:
        for m in await _overwrite_members(ch):
            if m.bot or (me and m.id==me.id) or m.id in targets: continue
            if ch.overwrites_for(m).view_channel is not False: continue
            if _is_staff_member(m) and not (role and role in m.roles): continue
            targets[m.id]=m
    except Exception as e:
        print(f'[REOPEN] 讀取頻道覆寫失敗｜channel={ch.id}｜error={e!r}')
    restored=[]; failed=[]
    for m in targets.values():
        try:
            await asyncio.wait_for(ch.set_permissions(m,view_channel=True,send_messages=True,read_message_history=True,
                attach_files=True,embed_links=True,reason='CT小舖 工單重新開放：恢復存取權限'),timeout=15)
            restored.append(m.display_name)
        except (discord.Forbidden,discord.HTTPException,asyncio.TimeoutError) as e:
            failed.append(m.display_name); print(f'[REOPEN] 恢復失敗｜channel={ch.id}｜member={m.id}｜error={e!r}')
    return restored, failed

def _unit_of(order):
    """數量單位：子彈是「發」，其他是「隻」。"""
    try: return '發' if order['service_type']==BULLET_SERVICE else '隻'
    except (KeyError, IndexError, TypeError): return '隻'

def _svc_label(service_type):
    """頻道名稱裡的服務字樣。"""
    if service_type == '代肝': return '代肝'
    if service_type == BULLET_SERVICE: return '子彈'
    return '幣號'

def rename_name(status,no,product,qty,buyer=None,service_type='幣號'):
    # 統一名稱格式：狀態-服務類型-工單號-額度
    st = _svc_label(service_type)
    if st == '子彈':
        try: qn = max(1, int(qty))
        except (TypeError, ValueError): qn = 1
        return f'{status}-{st}-{no}-{qn}發'
    amount = str(product or '').strip()
    m = re.search(r'(\d+(?:\.\d+)?)\s*M', amount, re.I)
    if not m:
        try:
            amount = f'{int(qty)}M'
        except Exception:
            amount = '50M'
    else:
        amount = f'{m.group(1)}M'
    if st == '幣號':
        try:
            qn = max(1, int(qty))
        except (TypeError, ValueError):
            qn = 1
        return f'{status}-{st}-{no}-{amount}{qn}隻'
    return f'{status}-{st}-{no}-{amount}'


async def sync_ticket_state_after_rename(ch, old_status, new_status, buyer_id, service_type):
    """/改名工單、/修改狀態 共用：改名後的權限同步。
    只改名稱與權限，**不移動頻道**——要移分類請用 /手動操作分流、/批量移動工單，或 /關閉工單。"""
    old_closed = str(old_status or '') == '結單'
    new_closed = str(new_status or '') == '結單'
    if new_closed:
        return '\n📁 只改了名稱，沒有移動頻道（要移動請用 `/手動操作分流`；要關閉並移入結單分類請用 `/關閉工單`）'
    if old_closed and not new_closed:
        try:
            _restored,failed=await restore_access(ch, buyer_id or None)
            if failed: print(f'[RENAME] 恢復權限失敗：{failed}')
        except Exception as e: print(f'[RENAME] restore access failed｜channel={ch.id}｜error={e!r}')
    return ''

async def rename_order_channel(order, guild, reason='訂單狀態自動改名'):
    # 訂單成立／狀態變更時自動同步工單名稱，永遠不加入客人名稱。
    try:
        ch=guild.get_channel(int(order['channel_id']))
    except (TypeError, ValueError):
        return False
    if not isinstance(ch,discord.TextChannel):
        return False
    name=rename_name(order['status'],order['ticket_no'],order['product'],order['quantity'],service_type=order['service_type'] if 'service_type' in order.keys() else '幣號')
    if ch.name==name:
        return True
    return await _safe_edit_channel(ch,timeout=10,name=name,reason=reason)

def support_role(guild):
    """客服身分組：優先用 /設定客服身分組，沒設定就用 /設定開單身分組 的身分組；都沒有回傳 None。"""
    for k in ('support_role_id','ticket_staff_role_id'):
        v=str(get(guild.id,k,'') or '')
        if v.isdigit():
            r=guild.get_role(int(v))
            if r is not None: return r
    return None

def payment_info(g):
    vals=[('銀行',get(g,'pay_bank')),('代碼',get(g,'pay_code')),('帳號',get(g,'pay_account')),('戶名',get(g,'pay_name'))]
    s='\n'.join(f'{a}：{b}' for a,b in vals if b)
    return s or '目前尚未設定付款資訊，請聯絡店長。'

def payment_info_md(g):
    """付款資訊：代碼／帳號／戶名各自放在程式碼區塊，客人可單獨複製。"""
    parts=[]
    bank=get(g,'pay_bank')
    if bank: parts.append(f'🏦 **銀行**：{bank}')
    for label,val in (('代碼',get(g,'pay_code')),('帳號',get(g,'pay_account')),('戶名',get(g,'pay_name'))):
        if val: parts.append(f'**{label}**\n```\n{val}\n```')
    return '\n'.join(parts) or '目前尚未設定付款資訊，請聯絡店長。'

def template(g,k,default): return get(g,k,default)

def foreign_ticket_notice(i,ch):
    """購買面板只該由開這張工單的客人操作。若按的人不是工單客人，回傳要顯示的提示（沒問題回傳 None）。
    以前沒有這個檢查，店長在客人的工單裡按一下商品按鈕，訂單就記成店長的、進度卡也 @ 店長。"""
    try: rec=ticket_record(ch)
    except Exception: return None
    bid=str(rec['buyer_id']) if rec and rec['buyer_id'] else ''
    if bid.isdigit() and bid!=str(i.user.id):
        # 自我修復：工單上記的「客人」其實是店內人員（舊版誤判的結果），而現在按的是一般客人 → 以按的人為準並更正。
        try:
            recorded=i.guild.get_member(int(bid))
            if recorded is not None and _is_staff_member(recorded) and not _is_staff_member(i.user):
                q('UPDATE ticket_channels SET buyer_id=?,buyer_name=? WHERE channel_id=?',(str(i.user.id),i.user.display_name,str(ch.id)))
                return None
        except Exception: traceback.print_exc()
        return f'❌ 這張工單是 <@{bid}> 開的，購買面板請由本人操作。\n（如果工單客人記錯了，店長可以用 `/設定工單客人` 更正。）'
    return None

def shop_paused(guild_id):
    """店長用 /暫停接單 後為 True。以前只寫入狀態、下單時從來沒檢查，所以「暫停接單」其實沒有擋住任何人。"""
    return str(get(guild_id,'shop_status','營業中')).strip()=='暫停接單'

def paused_message(guild_id):
    msg=template(guild_id,'paused_text','⏸️ **目前暫停接單**\n暫時無法建立新訂單，請稍後再來，或等待店長恢復接單。')
    rest=str(get(guild_id,'rest_text','')).strip()
    return msg+(f'\n\n📢 {rest}' if rest else '')

def restock_line(guild_id,product):
    t=str(get(guild_id,f'restock_{product}','')).strip()
    return f'\n📦 預計補貨時間：**{t}**' if t else ''

async def post_channel_log(guild, setting_key, text):
    cid=get(guild.id,setting_key)
    if not cid: return
    ch=guild.get_channel(int(cid))
    if not isinstance(ch,discord.TextChannel): return
    try: await ch.send(text)
    except discord.HTTPException: pass

async def _status_announce_unlocked(order, guild):
    """建立或更新工單狀態頻道中的訂單卡。"""
    try:
        cid=get(guild.id,'status_channel_id','').strip()
        if not cid:
            print(f'[STATUS] 尚未設定狀態頻道｜guild={guild.id}｜order={order["id"]}')
            return False
        try:
            ch=guild.get_channel(int(cid))
        except (TypeError, ValueError):
            ch=None
        if ch is None:
            try:
                ch=await guild.fetch_channel(int(cid))
            except Exception as e:
                print(f'[STATUS] 找不到狀態頻道｜guild={guild.id}｜channel={cid}｜error={e!r}')
                return False
        if not isinstance(ch,discord.TextChannel):
            print(f'[STATUS] 設定的頻道不是文字頻道｜guild={guild.id}｜channel={cid}')
            return False

        buyer_name='客人'
        if order['buyer_id']:
            try:
                buyer=guild.get_member(int(order['buyer_id']))
                if buyer:
                    buyer_name=buyer.display_name
            except (TypeError, ValueError):
                pass
        if buyer_name=='客人':
            rec=q('SELECT buyer_name FROM ticket_channels WHERE channel_id=?',(str(order['channel_id']),),True)
            if rec and rec[0]['buyer_name']:
                buyer_name=rec[0]['buyer_name']

        delivery=order['delivery_time'] or get(guild.id,'delivery_time','') or '現貨'
        payment=order['payment_method'] or '尚未選擇'
        custom=template(guild.id,'status_template','')
        desc=''
        if custom:
            try:
                desc=safe_format(custom,ticket=order['ticket_no'],status=order['status'],product=order['product'],quantity=order['quantity'],total=f"{order['total_price']:,}",customer=buyer_name,delivery=delivery,payment_method=payment)
            except Exception as e:
                print(f'[STATUS] 自訂狀態格式錯誤｜order={order["id"]}｜error={e!r}')
        if not desc:
            desc='請依照訂單狀態處理此筆訂單。'

        # 精簡小卡片：標題＝狀態＋工單號，內文兩行摘要，按鈕有「🎫 前往工單」
        if order['service_type']=='代肝':
            item=f'🛠️ 代肝｜{order["product"]}'
        elif order['product'].startswith('不死號'):
            item=f'📦 {order["product"]}'
        else:
            item=f'📦 {order["product"]} × {order["quantity"]} {_unit_of(order)}'
        lines=[f'👤 {buyer_name or "客人"}｜{item}｜💰 **NT${order["total_price"]:,}**',
               f'💳 {payment}｜🕐 {delivery}']
        if custom and desc: lines.append(clip(desc,500))
        emb=discord.Embed(title=f'{status_icon(order["status"])} #{order["ticket_no"]}｜{order["status"]}',description='\n'.join(lines),color=status_color(order['status']),timestamp=datetime.now(timezone.utc))
        emb.set_footer(text=f'訂單 ID {order["id"]}')

        old=q('SELECT message_id,channel_id FROM status_posts WHERE order_id=?',(order['id'],),True)
        try:
            if old:
                try:
                    msg=await ch.fetch_message(int(old[0]['message_id']))
                    await msg.edit(content=None,embed=emb,view=OrderManageView(int(order['id'])))
                except discord.NotFound:
                    msg=await ch.send(embed=emb,view=OrderManageView(int(order['id'])))
                    q('UPDATE status_posts SET channel_id=?,message_id=?,updated_at=? WHERE order_id=?',(str(ch.id),str(msg.id),now(),order['id']))
                else:
                    q('UPDATE status_posts SET channel_id=?,updated_at=? WHERE order_id=?',(str(ch.id),now(),order['id']))
            else:
                msg=await ch.send(embed=emb,view=OrderManageView(int(order['id'])))
                q('INSERT OR REPLACE INTO status_posts(order_id,channel_id,message_id,updated_at) VALUES(?,?,?,?)',(order['id'],str(ch.id),str(msg.id),now()))
            return True
        except discord.Forbidden as e:
            print(f'[STATUS] 沒有權限在狀態頻道發送/編輯訊息｜guild={guild.id}｜channel={ch.id}｜order={order["id"]}｜error={e!r}')
            return False
        except discord.HTTPException as e:
            print(f'[STATUS] Discord API 錯誤｜guild={guild.id}｜channel={ch.id}｜order={order["id"]}｜error={e!r}')
            return False
        except Exception as e:
            print(f'[STATUS] 未預期錯誤｜guild={guild.id}｜channel={ch.id}｜order={order["id"]}｜error={e!r}')
            traceback.print_exc()
            return False
    except Exception as e:
        print(f'[STATUS] 狀態公告流程錯誤｜order={order.get("id") if hasattr(order,"get") else "?"}｜error={e!r}')
        traceback.print_exc()
        return False

PROGRESS_STEPS=['下單','付款','店長確認','交付','完成']
PROGRESS_CUR={'待付款':1,'待確認付款':2,'待交貨':3,'待排單':3,'處理中':3,'待收貨':4,'結單':5}
PROGRESS_HINT={'待付款':'請完成付款，按「💳 完成付款」並上傳付款成功明細。','待確認付款':'已收到付款通知，店長確認中，請稍候。',
               '待交貨':'付款已確認，店長準備交付中。','待排單':'付款已確認，排單處理中。','處理中':'店長處理中，請稍候。',
               '待收貨':'已交付，請確認內容；有問題請直接在此告知店長。','結單':'訂單完成，感謝您的購買！'}

def progress_embed(o):
    st=o['status']; cur=PROGRESS_CUR.get(st,1); lines=[]
    for n,label in enumerate(PROGRESS_STEPS):
        if n<cur: lines.append(f'✅ {label}')
        elif n==cur: lines.append(f'⏳ **{label}**　◀ 目前')
        else: lines.append(f'▫️ {label}')
    e=discord.Embed(title=f'📍 訂單進度｜#{o["ticket_no"]}',description='\n'.join(lines)+f'\n\n{PROGRESS_HINT.get(st,"")}',color=status_color(st))
    e.set_footer(text=f'{st}｜{o["product"]}'); return e

async def progress_update(order, guild):
    """工單內的客人進度卡：狀態改變時刪掉舊卡、在最下面重發一張（同一張訂單永遠只有一張）。"""
    st=order['status']
    if st not in PROGRESS_CUR and st!='已取消': return
    try: ch=guild.get_channel(int(order['channel_id']))
    except (TypeError,ValueError): return
    if not isinstance(ch,discord.TextChannel): return
    old=q('SELECT * FROM progress_posts WHERE order_id=?',(order['id'],),True)
    if old and old[0]['status']==st: return
    if old:
        try: await ch.get_partial_message(int(old[0]['message_id'])).delete()
        except (discord.HTTPException,discord.NotFound,discord.Forbidden,ValueError): pass
        q('DELETE FROM progress_posts WHERE order_id=?',(order['id'],))
    if st=='已取消': return
    try: msg=await ch.send(embed=progress_embed(order))
    except discord.HTTPException as e:
        print(f'[PROGRESS] 發送失敗｜order={order["id"]}｜{e!r}'); return
    q('INSERT OR REPLACE INTO progress_posts(order_id,channel_id,message_id,status) VALUES(?,?,?,?)',(order['id'],str(ch.id),str(msg.id),st))

async def status_announce(order, guild):
    # 同一訂單的狀態更新可能由付款、管理員按鈕、結單等流程同時觸發；
    # 用訂單級鎖避免兩條流程都判斷「沒有舊訊息」而重複發送狀態卡。
    oid=str(order['id'])
    lk=STATUS_POST_LOCKS.setdefault(oid, asyncio.Lock())
    try:
        async with lk:
            res=await _status_announce_unlocked(order, guild)
            try: await progress_update(order, guild)
            except Exception: traceback.print_exc()
            return res
    finally:
        if STATUS_POST_LOCKS.get(oid) is lk:
            STATUS_POST_LOCKS.pop(oid,None)

def log_order(order_id,gid,op,action,detail=''): q('INSERT INTO order_logs(order_id,guild_id,operator_id,action,detail,created_at) VALUES(?,?,?,?,?,?)',(str(order_id),str(gid),str(op) if op else None,action,detail,now()))

async def remove_obsolete_bot_panels(guild):
    """刪除目前版本以前留下的互動面板。只處理本 Bot 自己發出的訊息。

    現行持久化互動 custom_id 統一以 ct_ 開頭；ephemeral/短生命週期按鈕不會出現在頻道歷史。
    因此舊版使用 vXX_*、rename_* 等 custom_id 的面板可安全清除，不碰其他 Bot。
    """
    removed=0
    bot_user_id=getattr(getattr(bot,'user',None),'id',None)
    if not bot_user_id:
        return 0
    for ch in list(getattr(guild,'text_channels',[]) or []):
        try:
            async for msg in ch.history(limit=2000, oldest_first=True):
                if getattr(getattr(msg,'author',None),'id',None) != bot_user_id:
                    continue
                components=getattr(msg,'components',[]) or []
                if not components:
                    continue
                ids=[]; has_link=False
                for row in components:
                    for child in (getattr(row,'children',[]) or []):
                        cid=getattr(child,'custom_id',None)
                        if cid: ids.append(str(cid))
                        elif getattr(child,'url',None): has_link=True
                # 舊版面板的元件沒有 ct_ 開頭的 custom_id → 刪除。
                # 例外：只有「🎫 前往工單」連結按鈕（沒有 custom_id、有網址）的是狀態頻道的訂單卡（待付款／結單／已取消），
                # 以前會被當成舊面板，每次機器人重啟就被刪光。
                if (not ids and not has_link) or any(not x.startswith('ct_') for x in ids):
                    try:
                        await msg.delete(reason='CT小舖 清理舊版互動面板')
                        removed += 1
                    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                        pass
        except (discord.HTTPException, discord.Forbidden):
            continue
    return removed

async def send_purchase_panel(ch, force=False):
    """新工單唯一自動顯示的購買服務面板；不會顯示改名工單面板。"""
    if not isinstance(ch, discord.TextChannel): return False
    no=ticket_no(ch.name)
    if no is None:
        rec=ticket_record(ch)
        no=rec['ticket_no'] if rec else None
    if no is None: return False
    # 已有購買服務面板就不重複發送（force=True 的「重新選擇」流程除外，舊面板已先刪掉）。
    try:
        async for msg in (ch.history(limit=100, oldest_first=True) if not force else _empty_aiter()):
            if not getattr(getattr(msg, 'author', None), 'bot', False):
                continue
            ids=[getattr(child,'custom_id',None) for row in (getattr(msg,'components',[]) or []) for child in (getattr(row,'children',[]) or [])]
            if 'ct_service:coin' in ids or 'ct_service:boost' in ids or 'ct_service:bullet' in ids:
                return True
    except discord.HTTPException:
        pass
    desc='請先選擇您要購買的服務 👇\n\n🪙 **幣號**\n▸ 購買遊戲幣號（100M 普通／不死）\n\n🛠️ **代肝**\n▸ 選擇代肝額度並付款排單\n\n🔫 **A大子彈**\n▸ 限定子彈，一發一發購買'
    if off_hours(ch.guild.id):
        desc += '\n\n🕐 **目前為非營業時間**\n目前可以正常下單及付款，但付款後會等店長回來再處理。'+off_hours_back_line(ch.guild.id)
    emb=discord.Embed(title='🛒 三角洲交易系統',description=desc,color=UI_COLOR['brand'])
    brand_footer(emb,f'工單 #{no}')
    try:
        await ch.send(content='🛒 三角洲交易系統',embed=emb,view=ServiceTypeView())
        return True
    except discord.HTTPException as e:
        print('[PURCHASE_PANEL] 發送失敗:',repr(e))
        return False

async def _empty_aiter():
    return
    yield

async def _delete_old_panels(ch, oid=None):
    """刪掉工單裡本 Bot 發的舊「購買服務」面板（有 ct_service 按鈕的訊息）。"""
    try:
        async for msg in ch.history(limit=100):
            if not getattr(getattr(msg,'author',None),'id',None)==bot.user.id: continue
            ids=[getattr(c,'custom_id',None) for row in (getattr(msg,'components',[]) or []) for c in (getattr(row,'children',[]) or [])]
            pay_ids=[x for x in ids if oid is not None and str(x or '').startswith(('ct_pay','ct_paid','ct_cancel','ct_reset')) and str(x).endswith(f':{oid}')]
            if pay_ids or any(x in ids for x in ('ct_service:coin','ct_service:boost','ct_service:bullet')):
                try: await msg.delete()
                except (discord.HTTPException,discord.NotFound,discord.Forbidden): pass
    except (discord.HTTPException,discord.Forbidden): pass

async def reset_pending_order(i, oid):
    """待付款階段的「🔄 重新選擇」：刪除原訂單（庫存退回）、清掉舊面板、重發新的購買面板。"""
    try: await i.response.defer()
    except discord.InteractionResponded: pass
    r=q('SELECT * FROM orders WHERE id=?',(oid,),True)
    if not r: return await i.followup.send('ℹ️ 這筆訂單已不存在，請使用工單裡最新的購買面板。',ephemeral=True)
    o=r[0]
    if str(o['buyer_id'])!=str(i.user.id) and not admin(i.user):
        return await i.followup.send('❌ 這不是您的訂單，無法操作。',ephemeral=True)
    if o['status']!='待付款':
        return await i.followup.send('ℹ️ 這筆訂單已送出付款通知，無法重新選擇，請聯絡店長處理。',ephemeral=True)
    sp=q('SELECT channel_id,message_id FROM status_posts WHERE order_id=?',(oid,),True)
    with db:
        cur=db.cursor()
        cur.execute("DELETE FROM orders WHERE id=? AND status='待付款'",(oid,))
        if cur.rowcount!=1:
            return await i.followup.send('ℹ️ 這筆訂單狀態剛剛變了，無法重新選擇。',ephemeral=True)
        if int(o['stock_reserved'] or 0)==1:
            cur.execute('UPDATE products SET stock=stock+? WHERE product=?',(int(o['quantity']),o['product']))
        cur.execute('DELETE FROM status_posts WHERE order_id=?',(oid,))
    log_order(oid,i.guild.id,i.user.id,'重新選擇',f'待付款訂單已刪除：{o["product"]} × {o["quantity"]}｜NT${o["total_price"]:,}')
    pp=q('SELECT channel_id,message_id FROM progress_posts WHERE order_id=?',(oid,),True)
    if pp:
        try: await i.channel.get_partial_message(int(pp[0]['message_id'])).delete()
        except (discord.HTTPException,discord.NotFound,discord.Forbidden,ValueError,AttributeError): pass
        q('DELETE FROM progress_posts WHERE order_id=?',(oid,))
    # 刪掉狀態頻道裡那張訂單卡
    if sp:
        try:
            sch=i.guild.get_channel(int(sp[0]['channel_id']))
            if sch is not None: await sch.get_partial_message(int(sp[0]['message_id'])).delete()
        except (discord.HTTPException,discord.NotFound,discord.Forbidden,ValueError): pass
    await post_channel_log(i.guild,'order_log_channel_id',f'🔄 **客人重新選擇**｜#{o["ticket_no"]}｜原訂單 {o["product"]} × {o["quantity"]} 已刪除｜{i.user.mention}')
    ch=i.channel
    if not isinstance(ch,discord.TextChannel):
        return await i.followup.send('✅ 原訂單已刪除，請回到工單重新操作。',ephemeral=True)
    # 刪掉目前這則付款訊息（若是隱藏訊息刪不掉，就改成一行說明並拿掉按鈕）
    try: await i.message.delete()
    except (discord.HTTPException,discord.NotFound,discord.Forbidden,AttributeError):
        try: await i.edit_original_response(content='🔄 已取消原訂單，請使用下方新的購買面板重新選擇。',embed=None,view=None)
        except discord.HTTPException: pass
    await _delete_old_panels(ch,oid)
    # 頻道名稱還原成剛開單時的樣子
    name=f'工單-{o["ticket_no"]}'
    if ch.name!=name: await _safe_edit_channel(ch,timeout=10,name=name,reason='客人重新選擇，還原工單名稱')
    ok=await send_purchase_panel(ch,force=True)
    if not ok: await i.followup.send('⚠️ 原訂單已刪除，但新面板送出失敗，請通知店長。',ephemeral=True)

class ServiceTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        b=discord.ui.Button(label='🪙 幣號',style=discord.ButtonStyle.primary,custom_id='ct_service:coin')
        b.callback=show_coin_menu; self.add_item(b)
        b=discord.ui.Button(label='🛠️ 代肝',style=discord.ButtonStyle.primary,custom_id='ct_service:boost')
        b.callback=show_boost_menu; self.add_item(b)
        b=discord.ui.Button(label='🔫 A大子彈',style=discord.ButtonStyle.primary,custom_id='ct_service:bullet')
        b.callback=show_bullet_menu; self.add_item(b)
        b=discord.ui.Button(label='❓ 常見問題',style=discord.ButtonStyle.secondary,custom_id='ct_faq')
        b.callback=show_faq; self.add_item(b)

async def show_faq(i):
    text=(get(i.guild.id,'faq_text','') or '').strip()
    e=discord.Embed(title='❓ 常見問題',description=text or '目前店長還沒有設定常見問題，有疑問請直接在工單詢問。',color=UI_COLOR['info']); brand_footer(e)
    await i.response.send_message(embed=e,ephemeral=True)

async def show_bullet_menu(i):
    if not isinstance(i.channel,discord.TextChannel):
        return await i.response.send_message('❌ 請在工單頻道使用。',ephemeral=True)
    st=availability(BULLET_PRODUCT)
    if st=='暫停提供': return await i.response.send_message('⚠️ **A大子彈目前暫停提供**',ephemeral=True)
    if st=='缺貨': return await i.response.send_message('🔴 **A大子彈目前缺貨**',ephemeral=True)
    if price(BULLET_PRODUCT)<=0: return await i.response.send_message('❌ A大子彈目前尚未設定價格，請聯絡店長。',ephemeral=True)
    await i.response.send_modal(QuantityModal(BULLET_PRODUCT,i.message.id))

async def show_coin_menu(i):
    if not isinstance(i.channel,discord.TextChannel):
        return await i.response.send_message('❌ 請在工單頻道使用。',ephemeral=True)
    desc=f'📋 幣號價目表：{channel_link(i.guild,"幣號價目表")}\n\n請選擇您要的幣號規格。\n🟢 正常提供｜🟡 暫停提供｜🔴 缺貨'
    e=discord.Embed(title='🪙 幣號',description=desc,color=UI_COLOR['money'])
    await i.response.edit_message(content=None,embed=e,view=CoinProductView())

async def show_boost_menu(i):
    if not isinstance(i.channel,discord.TextChannel):
        return await i.response.send_message('❌ 請在工單頻道使用。',ephemeral=True)
    lines=[f'📋 代肝價目表：{channel_link(i.guild,"代肝價目表")}','\n請選擇代肝額度：']
    for t in BOOST_TIERS:
        v=boost_price(i.guild.id,t); lines.append(f'▸ **{t}**　NT${v:,}' if v>0 else f'▸ **{t}**　尚未設定')
    e=discord.Embed(title='🛠️ 代肝',description='\n'.join(lines),color=UI_COLOR['info'])
    await i.response.edit_message(content=None,embed=e,view=BoostTierView())

class OpenTicketView(discord.ui.View):
    """開單面板：單一「🎫 開始開單」按鈕，按下直接建立工單（不再選自助／真人客服）；客服身分組本來就看得到所有工單。"""
    def __init__(self):
        super().__init__(timeout=None)
        b=discord.ui.Button(label='🎫 開始開單',style=discord.ButtonStyle.primary,custom_id='ct_open_ticket'); b.callback=self.open_self; self.add_item(b)
    async def open_self(self,i): return await self.open_ticket(i,'self')
    async def open_ticket(self,i,mode='self'):
        # 按鈕回呼第一時間就 ACK，避免排隊鎖／Discord API 延遲導致「交互失敗」。
        if not i.guild or not isinstance(i.user, discord.Member):
            return await i.response.send_message('❌ 無法在此使用。',ephemeral=True)
        try:
            await i.response.defer(ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as e:
            print('[TICKET] open initial ACK failed:',repr(e))
            return
        key=(i.guild.id,i.user.id)
        lk=OPEN_TICKET_LOCKS.setdefault(key, asyncio.Lock())
        if lk.locked():
            return await i.followup.send('⏳ 正在建立您的工單，請稍候，不要重複按開單。',ephemeral=True)
        async with lk:
            try:
                return await self._open_ticket_impl(i,mode)
            finally:
                OPEN_TICKET_LOCKS.pop(key,None)

    async def _open_ticket_impl(self,i,mode='self'):
        # open_ticket 已經完成 Interaction ACK；後續一律使用 followup。
        acknowledged=True
        try:
            if not i.guild or not isinstance(i.user,discord.Member):
                return await i.followup.send('❌ 無法在此使用。',ephemeral=True)

            # 先確認機器人本身有建立頻道與設定權限的能力。
            me=i.guild.me
            if me is None:
                return await i.followup.send('❌ 找不到機器人的伺服器身分，請稍後再試。',ephemeral=True)
            perms=(i.channel.permissions_for(me) if isinstance(i.channel,discord.TextChannel) else i.guild.me.guild_permissions)
            if not perms.manage_channels:
                return await i.followup.send('❌ 機器人缺少「管理頻道」權限，無法建立工單。',ephemeral=True)

            # 預設同一位客人只能保留一張未結單工單；店長開啟「無限制開單」後，
            # 這一層限制完全跳過，但仍保留 OPEN_TICKET_LOCKS 防止同一時間快速連點造成重複建立。
            unlimited_open = get(i.guild.id,'unlimited_ticket_opening','0') == '1'
            if not unlimited_open:
                checked=set()
                for r in q('SELECT channel_id FROM ticket_channels WHERE guild_id=? AND buyer_id=?',(str(i.guild.id),str(i.user.id)),True):
                    try: ch0=i.guild.get_channel(int(r['channel_id']))
                    except (TypeError,ValueError): ch0=None
                    if not isinstance(ch0,discord.TextChannel):
                        continue
                    checked.add(ch0.id)
                    # 歷史 ticket_channels 不等於目前開啟中的工單；以最新訂單狀態 + Discord 頻道狀態共同判斷。
                    active_order=q("SELECT status FROM orders WHERE channel_id=? AND buyer_id=? ORDER BY id DESC LIMIT 1",(str(ch0.id),str(i.user.id)),True)
                    if active_order and active_order[0]['status'] in ('結單','已取消'):
                        continue
                    if looks_closed(ch0.name):
                        continue
                    return await i.followup.send(f'❌ 你目前已有開啟中的工單：{ch0.mention}',ephemeral=True)
                for ch0 in i.guild.text_channels:
                    if ch0.id in checked or not is_ticket_candidate(ch0) or looks_closed(ch0.name):
                        continue
                    try:
                        if ch0.permissions_for(i.user).view_channel:
                            # 只有能被該會員實際看見的「工單候選」才視為既有工單。
                            remember(ch0,ticket_no(ch0.name) or '0',i.user.id,i.user.display_name)
                            return await i.followup.send(f'❌ 你目前已有開啟中的工單：{ch0.mention}',ephemeral=True)
                    except Exception:
                        continue

            async with ticket_open_lock:
                try:
                    category=await pick_ticket_category(i.guild)
                except Exception as e:
                    print(f'[TICKET_CATEGORY] 自動建立下一個分類失敗｜guild={i.guild.id}｜error={e!r}')
                    return await i.followup.send('❌ 目前的工單分類已滿，而且自動建立下一個分類失敗，請確認機器人有「管理頻道」權限後再試。',ephemeral=True)
                if not isinstance(category,discord.CategoryChannel):
                    return await i.followup.send('❌ 店長尚未設定「開單分類」（或設定的分類已被刪除／被當成結單分類），暫時無法開單。請通知店長使用 `/設定開單分類`。',ephemeral=True)

                no=await allocate_ticket_no(i.guild.id, i.guild)
                ow={
                    i.guild.default_role:discord.PermissionOverwrite(view_channel=False),
                    i.user:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True)
                }
                owner=get(i.guild.id,'ticket_owner_id',''); rid=get(i.guild.id,'ticket_staff_role_id','')
                if owner.isdigit() and (m:=i.guild.get_member(int(owner))):
                    ow[m]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,manage_messages=True,attach_files=True,embed_links=True)
                if rid.isdigit() and (role:=i.guild.get_role(int(rid))):
                    ow[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True,embed_links=True)
                sup=support_role(i.guild)
                if sup is not None and sup not in ow:
                    ow[sup]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True,embed_links=True)
                ow[me]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,manage_channels=True,manage_messages=True,attach_files=True,embed_links=True)

                try:
                    ch=await i.guild.create_text_channel(f'ticket-{no}',category=category,overwrites=ow,reason='CT小舖開單')
                except Exception:
                    await release_ticket_no(i.guild.id,no)   # 頻道沒建成，把號碼退回去，避免跳號
                    raise
            remember(ch,no,i.user.id,i.user.display_name)
            # 第一則：歡迎訊息 + 🔒 關閉工單按鈕（文字可用 /設定工單訊息 修改）。
            welcome_ok=True
            try:
                welcome=get(i.guild.id,'ticket_welcome','') or '👋 歡迎開單！請稍候，店長會盡快協助您。'
                await ch.send(content=f'{i.user.mention}\n{welcome}'[:2000],view=WelcomeCloseView())
            except discord.HTTPException as e:
                print('[TICKET] welcome send error:',repr(e)); welcome_ok=False
            # 第二則：「購買服務」面板（幣號／代肝 → 額度／數量 → 付款）；可用 /停止發送面板 關閉。
            panel_on=get(i.guild.id,'purchase_panel_enabled','1')!='0'
            panel_ok=await send_purchase_panel(ch) if panel_on else True
            try:
                await ch.edit(name=f'工單-{no}',reason='新工單自動改名')
            except discord.HTTPException as e:
                print('[TICKET] rename error:',repr(e))
            # 新工單永遠留在「設定開單分類」，不因選擇幣號／代肝而移動。

            msg=f'✅ 工單已建立：{ch.mention}\n'+('購買服務面板已送出。' if panel_on else '購買服務面板目前已停止發送。')+'\n店長如需修改工單名稱，請使用 `/改名工單`。'
            if not welcome_ok:
                msg+='\n⚠️ 歡迎訊息（含關閉工單按鈕）發送失敗，請檢查 Bot 的發送訊息權限。'
            if not panel_ok:
                msg+='\n⚠️ 購買服務面板發送失敗，請檢查 Bot 的發送訊息權限。'
            await i.followup.send(msg,ephemeral=True)
        except discord.Forbidden as e:
            print('[TICKET] open forbidden:',repr(e))
            if acknowledged:
                await i.followup.send('❌ 機器人權限不足，無法完成開單。請確認機器人有「管理頻道」及「查看頻道／發送訊息」權限。',ephemeral=True)
            else:
                await i.response.send_message('❌ 機器人權限不足，無法完成開單。',ephemeral=True)
        except discord.HTTPException as e:
            print('[TICKET] open HTTP error:',repr(e))
            if acknowledged:
                await i.followup.send('❌ Discord 暫時拒絕了開單操作，請稍後再按一次。',ephemeral=True)
            else:
                await i.response.send_message('❌ Discord 暫時拒絕了開單操作，請稍後再試。',ephemeral=True)
        except Exception as e:
            print('[TICKET] open unexpected error:',repr(e))
            traceback.print_exc()
            if acknowledged:
                await i.followup.send('❌ 開單時發生程式錯誤，已記錄錯誤資訊，請通知店長。',ephemeral=True)
            else:
                await i.response.send_message('❌ 開單時發生程式錯誤，請通知店長。',ephemeral=True)

class LegacyOpenTicketView(discord.ui.View):
    """舊版開單面板（兩顆按鈕）：只為了讓已經發出去的舊面板不會失效而保留，兩顆都直接開單。"""
    def __init__(self):
        super().__init__(timeout=None)
        b=discord.ui.Button(label='🤖 自助下單',style=discord.ButtonStyle.success,custom_id='ct_open_ticket:self'); b.callback=self.go; self.add_item(b)
        b=discord.ui.Button(label='👤 真人客服',style=discord.ButtonStyle.primary,custom_id='ct_open_ticket:human'); b.callback=self.go; self.add_item(b)
    async def go(self,i): return await OpenTicketView().open_ticket(i,'self')

# 客人自己按「關閉工單」時，訂單必須是這些狀態之一（沒付款、已取消、已交貨、沒有訂單）。
# 已付款處理中的訂單（待確認付款／待排單／待交貨／處理中／有爭議／待倒）不能自行關閉：關閉會把訂單改成結單並退回預扣庫存。
BUYER_CLOSABLE_STATUSES=('待付款','待洽談','待處理','已取消','待收貨','結單')

def buyer_close_problem(ch):
    """客人（非管理員）能不能關這張工單？不能就回傳原因，可以就回傳 None。"""
    st=ticket_current_status(ch)
    if st in BUYER_CLOSABLE_STATUSES or st==CLOSE_ALL_NO_ORDER: return None
    return f'這張工單的訂單目前是「{st}」，進行中的訂單不能自行關閉，請聯絡管理員處理。'

async def ticket_close_prompt(i):
    """「🔒 關閉工單」按鈕共用：只有這張工單的客人或管理員可以關，先跳確認再關。
    （狀態變更仍只開放管理員，避免客人修改訂單狀態。）"""
    rec=ticket_record(i.channel) if isinstance(i.channel,discord.TextChannel) else None
    buyer_id=str(rec['buyer_id']) if rec and rec['buyer_id'] else ''
    if not admin(i.user) and str(i.user.id)!=buyer_id:
        return await i.response.send_message('❌ 只有這張工單的客人或管理員可以關閉工單。',ephemeral=True)
    if not admin(i.user):
        problem=buyer_close_problem(i.channel)
        if problem: return await i.response.send_message(f'❌ {problem}',ephemeral=True)
    text=('⚠️ **確定要關閉此工單嗎？**\n關閉後您將無法查看此工單，管理員仍可重新開放。' if str(i.user.id)==buyer_id and not admin(i.user) else '⚠️ **確定要關閉此工單嗎？**\n關閉後該客人將無法查看此工單，管理員仍可重新開放。')
    await i.response.send_message(text,view=CloseConfirmView(buyer_id=buyer_id),ephemeral=True)

class WelcomeCloseView(discord.ui.View):
    """新工單第一則訊息：只有關閉工單，避免一開單出現多個面板。"""
    def __init__(self):
        super().__init__(timeout=None)
        b = discord.ui.Button(label='🔒 關閉工單', style=discord.ButtonStyle.danger, custom_id='ct_welcome_close')
        b.callback = self.close
        self.add_item(b)

    async def close(self, i): await ticket_close_prompt(i)

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        b=discord.ui.Button(label='📋 工單狀況',style=discord.ButtonStyle.primary,custom_id='ct_ticket_status'); b.callback=self.status; self.add_item(b)
        b=discord.ui.Button(label='🔒 關閉工單',style=discord.ButtonStyle.danger,custom_id='ct_ticket_close'); b.callback=self.close; self.add_item(b)
    async def status(self,i):
        if not admin(i.user): return await deny(i)
        r=q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(i.channel.id),),True)
        if not r: return await i.response.send_message('📋 目前尚未建立訂單。',ephemeral=True)
        await i.response.send_message(embed=order_status_embed(r[0]),view=OrderStatusButtons(r[0]['id']),ephemeral=True)
    async def close(self,i): await ticket_close_prompt(i)

class CloseConfirmView(discord.ui.View):
    def __init__(self,buyer_id=''):
        super().__init__(timeout=60)
        self.buyer_id=str(buyer_id or '')
        for label,style,cb in [('❌ 取消',discord.ButtonStyle.secondary,self.cancel),('🔒 確認關閉',discord.ButtonStyle.danger,self.confirm)]:
            b=discord.ui.Button(label=label,style=style); b.callback=cb; self.add_item(b)
    async def cancel(self,i): await i.response.edit_message(content='已取消關閉工單。',view=None)
    async def confirm(self,i):
        ch=i.channel; is_admin=admin(i.user)
        if not is_admin and str(i.user.id) != self.buyer_id:
            return await i.response.edit_message(content='❌ 你沒有權限關閉這張工單。',view=None)
        if not isinstance(ch,discord.TextChannel): return await i.response.edit_message(content='❌ 找不到工單。',view=None)
        if not is_admin:
            problem=buyer_close_problem(ch)      # 確認時再檢查一次：按下關閉之後訂單可能已經付款
            if problem: return await i.response.edit_message(content=f'❌ {problem}',view=None)
        problem=closable_ticket_problem(i.guild,ch)
        if problem: return await i.response.edit_message(content=f'❌ {problem}',view=None)
        await i.response.defer(ephemeral=True)
        rec=ticket_record(ch); buyer_id=(rec['buyer_id'] if rec else '') or self.buyer_id
        # 和 /關閉工單 共用同一段：移除客人權限 → 結單改名 → 移入結單分類 → 紀錄與公告。
        res=await close_ticket_channel(i.guild,ch,i.user,'關閉工單',True,buyer_hint=buyer_id)
        # 這裡是按鈕 Interaction：ephemeral 的原始回覆要用 edit_original_response()。
        try:
            await i.edit_original_response(content=f'✅ 工單已關閉，狀態已設為「結單」。\n{res["perm_msg"]}',view=None)
        except (discord.NotFound, discord.HTTPException) as e:
            print(f'[CLOSE_CONFIRM] 更新確認訊息失敗｜message={getattr(i.message, "id", None)}｜error={e!r}')

class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        b=discord.ui.Button(label='🔓 重新開放',style=discord.ButtonStyle.success,custom_id='ct_ticket_reopen'); b.callback=self.reopen; self.add_item(b)
        b=discord.ui.Button(label='🗑️ 刪除工單',style=discord.ButtonStyle.danger,custom_id='ct_ticket_delete'); b.callback=self.delete; self.add_item(b)
    async def reopen(self,i):
        if not admin(i.user): return await deny(i)
        await i.response.defer(ephemeral=True)
        buyer_id=(await buyer_for(i.channel))[0] if isinstance(i.channel,discord.TextChannel) else None
        try:
            restored,rfail=await restore_access(i.channel,buyer_id)
        except Exception as e:
            restored,rfail=[],[]; print(f'[TICKET_REOPEN] 恢復權限失敗｜channel={i.channel.id}｜error={e!r}')
        rows=q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(i.channel.id),),True)
        reopened_status=None
        if rows:
            o=rows[0]
            reopened_status='待處理'
            q('UPDATE orders SET status=?,completed_at=NULL WHERE id=?',(reopened_status,o['id']))
            rr=q('SELECT * FROM orders WHERE id=?',(o['id'],),True)[0]
            try:
                await asyncio.wait_for(i.channel.edit(name=rename_name(reopened_status,rr['ticket_no'],rr['product'],rr['quantity'],service_type=rr['service_type'] or '幣號'),reason='工單重新開放自動改名'),timeout=20)
            except (discord.Forbidden,discord.HTTPException,asyncio.TimeoutError) as e:
                q('UPDATE orders SET status=?,completed_at=? WHERE id=?',(o['status'],o['completed_at'],o['id']))
                print('[TICKET_REOPEN] 改名失敗，DB 已回復:',repr(e))
                return await i.followup.send(f'❌ 重新開放失敗，工單資料已回復：`{str(e)[:180]}`',ephemeral=True)
            await status_announce(rr,i.guild)
        try:
            open_cat=await get_ticket_creation_category(i.guild)
            if isinstance(open_cat,discord.CategoryChannel) and i.channel.category_id != open_cat.id:
                if len(open_cat.channels) < 50:
                    await i.channel.edit(category=open_cat,reason='工單重新開放移回開單分類')
        except (discord.Forbidden,discord.HTTPException) as e:
            print('[TICKET_REOPEN] 移回開單分類失敗:',repr(e))
        await i.followup.send(f'🔓 工單已重新開放，狀態：**{reopened_status or "待處理"}**。並移回開單分類。'+(f'\n已恢復權限：{"、".join(restored)}' if restored else '')+(f'\n⚠️ 恢復失敗：{"、".join(rfail)}' if rfail else ''),ephemeral=True)
    async def delete(self,i):
        if not strict_admin(i.user): return await deny(i, True)       # 刪除無法復原，只有店長等級
        problem=closable_ticket_problem(i.guild,i.channel)
        if problem: return await i.response.send_message(f'❌ {problem}',ephemeral=True)
        await i.response.send_message('⚠️ **確定要永久刪除此工單嗎？**（無法復原）',view=DeleteConfirmView(),ephemeral=True)

class DeleteConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        for label,style,cb in [('❌ 取消',discord.ButtonStyle.secondary,self.cancel),('🗑️ 確定刪除',discord.ButtonStyle.danger,self.confirm)]:
            b=discord.ui.Button(label=label,style=style); b.callback=cb; self.add_item(b)
    async def cancel(self,i): await i.response.edit_message(content='已取消刪除工單。',view=None)
    async def confirm(self,i):
        if not strict_admin(i.user): return await i.response.edit_message(content='❌ 只有店長等級可以刪除工單。',view=None)
        ch=i.channel
        problem=closable_ticket_problem(i.guild,ch)
        if problem: return await i.response.edit_message(content=f'❌ {problem}',view=None)
        await i.response.edit_message(content='🗑️ 正在刪除工單……',view=None)
        channel_id=str(ch.id); ch_name=ch.name; no=_resolve_ticket_number(ch) or '?'
        try:
            await ch.delete(reason=f'工單刪除｜{i.user}')
            TICKET_CHANNEL_IDS.discard(channel_id)
        except discord.HTTPException as e:
            print(f'[TICKET_DELETE] 刪除失敗｜channel={channel_id}｜error={e!r}')
            try: await i.edit_original_response(content='❌ 刪除失敗：機器人可能缺少「管理頻道」權限。')
            except discord.HTTPException: pass
            return
        await post_channel_log(i.guild,'action_log_channel_id',f'🗑️ **刪除工單**｜#{no}｜`{ch_name}`｜{i.user.mention}')

def order_status_embed(o):
    e=discord.Embed(title=f'📋 工單狀況｜#{o["ticket_no"]}',description='管理員可直接按下方按鈕更新狀態。',color=status_color(o['status']))
    e.add_field(name='服務',value=o['service_type'],inline=True); e.add_field(name='商品／額度',value=o['product'],inline=True); e.add_field(name='數量',value=f'{o["quantity"]} {_unit_of(o)}',inline=True)
    e.add_field(name='金額',value=f'NT${int(o["total_price"]):,}',inline=True); e.add_field(name='狀態',value=f"{status_icon(o['status'])} {o['status']}",inline=True); e.add_field(name='付款明細',value='已提供' if o['payment_proof_url'] else '尚未提供',inline=True)
    return e

class OrderStatusButtons(discord.ui.View):
    def __init__(self,oid):
        super().__init__(timeout=120); self.oid=oid
        for st in ['待確認付款','待排單','待交貨','處理中','待收貨','有爭議','已完成']:
            b=discord.ui.Button(label=st,style=discord.ButtonStyle.danger if st=='有爭議' else discord.ButtonStyle.primary); b.callback=self.make(st); self.add_item(b)
    def make(self,st):
        async def cb(i):
            if not admin(i.user): return await deny(i)
            await i.response.defer(ephemeral=True)
            r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
            if not r: return await i.followup.send('❌ 找不到訂單。',ephemeral=True)
            o=r[0]; new='結單' if st=='已完成' else st
            q('UPDATE orders SET status=?,completed_at=? WHERE id=?',(new,now() if new=='結單' else None,self.oid)); release_reserved_stock(self.oid,o['status'],new); refunded=refund_balance_on_cancel(self.oid,o['status'],new,i.user.id); log_order(self.oid,i.guild.id,i.user.id,'按鈕修改狀態',f'{o["status"]} → {new}'+(f'｜已退回餘額 NT${refunded:,}' if refunded else ''))
            rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await rename_order_channel(rr,i.guild,'工單狀態按鈕自動改名'); await status_announce(rr,i.guild)
            await i.followup.send(f'📋 **工單狀態更新**\n#{o["ticket_no"]}\n{o["status"]} → **{new}**'+(f'\n💰 已把餘額付款 NT${refunded:,} 退回客人的餘額。' if refunded else ''), ephemeral=True)
        return cb

class CoinProductView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for p in COIN_PRODUCTS:
            self.add_item(CoinProductButton(p))

class CoinProductButton(discord.ui.Button):
    def __init__(self,p):
        self.p=p
        st=availability(p) if p in [r['product'] for r in q('SELECT product FROM products',(),True)] else '正常提供'
        icon={'正常提供':'🟢','暫停提供':'🟡','缺貨':'🔴'}.get(st,'🟢')
        super().__init__(label=f'{icon} {p}',style=discord.ButtonStyle.primary,custom_id=f'ct_coin:{p}')
    async def callback(self,i):
        if shop_paused(i.guild.id): return await i.response.send_message(paused_message(i.guild.id),ephemeral=True)
        _fn=foreign_ticket_notice(i,i.channel)
        if _fn: return await i.response.send_message(_fn,ephemeral=True)
        st=availability(self.p)
        if st=='暫停提供': return await i.response.send_message('⚠️ **目前暫停提供**\n此規格目前暫時不提供，請選擇其他規格。'+restock_line(i.guild.id,self.p),ephemeral=True)
        if st=='缺貨': return await i.response.send_message(safe_format(template(i.guild.id,'out_of_stock','🔴 **目前缺貨**\n此規格目前沒有現貨，請選擇其他規格。'),product=self.p)+restock_line(i.guild.id,self.p),ephemeral=True)
        if '不死號' in self.p:
            await create_negotiation_order(i,self.p)
            return
        await i.response.send_modal(QuantityModal(self.p,i.message.id))

class BoostTierView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for t in BOOST_TIERS:
            b=discord.ui.Button(label=t,style=discord.ButtonStyle.primary,custom_id=f'ct_boost:{t}')
            b.callback=self.make_cb(t); self.add_item(b)
    def make_cb(self,tier):
        async def cb(i):
            await create_boost_order(i,tier)
        return cb

class QuantityModal(discord.ui.Modal,title='輸入購買數量'):
    # 與客人「自動購買 → 幣號 → 選擇要買幾隻」共用同一個原生 Discord 數量輸入視窗。
    qty=discord.ui.TextInput(label='購買數量',placeholder='請輸入數量，例如：7',min_length=1,max_length=6,required=True)
    def __init__(self,p=None,source_message_id=None):
        super().__init__()
        self.p=p
        self.source_message_id=int(source_message_id) if source_message_id is not None else None

    async def on_submit(self,i):
        try:
            n=int(str(self.qty.value).strip())
        except (TypeError,ValueError):
            return await i.response.send_message('❌ 數量請輸入整數，例如：7。',ephemeral=True)
        if not 1<=n<=999999:
            return await i.response.send_message('❌ 數量請輸入 1～999999 的整數。',ephemeral=True)

        # 客人自動購買的原本流程完全不變。
        await i.response.defer()
        await process_selection(i,self.p,n,source_message_id=self.source_message_id)


async def edit_source_message(i,source_message_id,*,content=None,embed=None,view=None):
    if source_message_id is None: return await i.edit_original_response(content=content,embed=embed,view=view)
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return False
    try:
        msg=ch.get_partial_message(int(source_message_id)); await msg.edit(content=content,embed=embed,view=view); return True
    except (discord.NotFound,discord.Forbidden,discord.HTTPException) as e: print('edit_source_message error:',repr(e)); return False

async def process_selection(i,p,n,source_message_id=None):
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return await i.followup.send('❌ 請在工單頻道操作。',ephemeral=True) if i.response.is_done() else await i.response.send_message('❌ 請在工單頻道操作。',ephemeral=True)
    rec=ticket_record(ch); no=rec['ticket_no'] if rec else ticket_no(ch.name)
    if not no: return await i.followup.send('❌ 暫時無法辨識工單編號，請稍後再試。',ephemeral=True)
    if shop_paused(ch.guild.id): return await i.followup.send(paused_message(ch.guild.id),ephemeral=True)
    _fn=foreign_ticket_notice(i,ch)
    if _fn: return await i.followup.send(_fn,ephemeral=True)
    if availability(p)!='正常提供': return await i.followup.send('❌ 此規格目前無法購買。'+restock_line(ch.guild.id,p),ephemeral=True)
    is_bullet=(p==BULLET_PRODUCT); svc=BULLET_SERVICE if is_bullet else '幣號'; u='發' if is_bullet else '隻'
    try: min_qty=int(get(ch.guild.id,f'min_purchase_qty:{p}','0') or 0)
    except (TypeError,ValueError): min_qty=0
    if min_qty>0 and n<min_qty:
        return await i.followup.send(f'❌ 此商品最低購買數量為 **{min_qty} {u}**，目前輸入 {n} {u}。',ephemeral=True)
    unit=price(p)
    if unit<=0: return await i.followup.send('❌ 這項商品目前尚未設定價格，請聯絡店長。',ephemeral=True)
    if is_bullet:
        # 子彈不限庫存：直接建立訂單。
        return await create_order_and_show_payment(i,p,n,unit,calc_total(unit,n),'',source_message_id=source_message_id,already_deferred=True,service_type=svc)
    s=stock(p)
    if s<n:
        delivery=get(ch.guild.id,'delivery_time','').strip()
        if not delivery: return await i.followup.send(safe_format(template(ch.guild.id,'not_enough_stock','❌ 目前 {product} 庫存不足，店長尚未設定交貨時間。'),product=p,stock=s,quantity=n)+restock_line(ch.guild.id,p),ephemeral=True)
        e=discord.Embed(title='📦 缺貨訂單確認',description=f'目前僅剩 {s} 隻現貨，您需要 {n} 隻。\n\n🕐 本店交貨時間：**{delivery}**\n\n請確認是否接受。',color=UI_COLOR['warn'])
        e.add_field(name='商品',value=p); e.add_field(name='數量',value=f'{n} 隻'); e.add_field(name='總價',value=f'NT${calc_total(unit,n):,}')
        ok=await edit_source_message(i,source_message_id,embed=e,view=DeliveryAcceptView(p,n,unit,calc_total(unit,n),delivery))
        if not ok:
            await i.followup.send(embed=e,view=DeliveryAcceptView(p,n,unit,calc_total(unit,n),delivery),ephemeral=True)
        return None
    return await create_order_and_show_payment(i,p,n,unit,calc_total(unit,n),'',source_message_id=source_message_id,already_deferred=True,service_type=svc)

async def create_order_and_show_payment(i,p,qty,unit,total,delivery,source_message_id=None,already_deferred=False,service_type='幣號'):
    if not already_deferred:
        try: await i.response.defer()
        except discord.InteractionResponded: pass
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return await i.edit_original_response(content='❌ 請在工單頻道操作。',embed=None,view=None)
    if shop_paused(ch.guild.id): return await i.edit_original_response(content=paused_message(ch.guild.id),embed=None,view=None)
    rec=ticket_record(ch); no=rec['ticket_no'] if rec else ticket_no(ch.name)
    if not no: return await i.edit_original_response(content='❌ 暫時無法辨識工單編號，請稍後再試。',embed=None,view=None)
    existing=q("SELECT * FROM orders WHERE channel_id=? AND buyer_id=? AND status NOT IN ('結單','已取消') ORDER BY id DESC LIMIT 1",(str(ch.id),str(i.user.id)),True)
    if existing: return await i.edit_original_response(content=f'ℹ️ 這張工單已有進行中的訂單 **#{existing[0]["ticket_no"]}**，目前狀態：**{existing[0]["status"]}**。',embed=None,view=None)
    if service_type=='幣號' and availability(p)!='正常提供': return await i.edit_original_response(content='❌ 此規格目前無法購買。',embed=None,view=None)
    stock_reserved=0
    with db:
        c=db.cursor()
        if service_type=='幣號' and not str(p).startswith('不死號'):
            # 只有現貨足夠時才預扣庫存；缺貨接受交貨時間的訂單不把庫存扣成負數。
            c.execute('UPDATE products SET stock=stock-? WHERE product=? AND stock>=?',(int(qty),p,int(qty)))
            if c.rowcount==1: stock_reserved=1
            elif int(stock(p))>=int(qty):
                raise sqlite3.IntegrityError('庫存在建立訂單瞬間發生競爭，請重新操作。')
        c.execute('INSERT INTO orders(ticket_no,channel_id,guild_id,product,quantity,unit_price,total_price,status,buyer_id,created_at,delivery_time,service_type,stock_reserved) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(no,str(ch.id),str(ch.guild.id),p,qty,unit,total,'待付款',str(i.user.id),now(),delivery,service_type,stock_reserved)); oid=c.lastrowid; db.commit()
    order=q('SELECT * FROM orders WHERE id=?',(oid,),True)[0]
    await rename_order_channel(order,ch.guild,'建立訂單後自動改名')
    await post_channel_log(ch.guild,'order_log_channel_id',f'🧾 **新{service_type}訂單**｜#{no}｜{p}｜NT${total:,}｜客人：{i.user.mention}')
    log_order(oid,ch.guild.id,i.user.id,'建立訂單',f'{service_type}｜{p}｜NT${total:,}')
    await status_announce(order,ch.guild)
    e=discord.Embed(title='💳 付款方式',description=safe_format(template(ch.guild.id,'pay_method_intro','本店付款方式為 {bank}\n\n請問您要使用的付款方式是？'),bank=get(ch.guild.id,'pay_bank','尚未設定')),color=UI_COLOR['money'])
    e.add_field(name='訂單',value=f'#{no}｜{p}｜NT${total:,}',inline=False)
    if off_hours(ch.guild.id): e.add_field(name='🕐 非營業時間',value='目前可以付款，但店長目前不在線，付款後會等店長回來確認。'+off_hours_back_line(ch.guild.id),inline=False)
    # 付款面板一定要是工單裡「最新的一則」：上面的 status_announce 已經在工單最下面發了「訂單進度」卡，
    # 如果還是去改舊的購買面板訊息，付款面板會被擠到進度卡上面、客人得往上翻才找得到按鈕。
    # 所以改成：在進度卡下面發一則新的付款面板，再把被取代的舊面板刪掉；發送失敗才退回原本「改舊訊息」的做法。
    new_panel=None
    try: new_panel=await ch.send(embed=e,view=PaymentMethodView(oid))
    except discord.HTTPException as ex: print(f'[PAYMENT_PANEL] 新面板發送失敗，改用編輯舊訊息｜{ex!r}')
    if new_panel is not None:
        try:
            if source_message_id is not None: await ch.get_partial_message(int(source_message_id)).delete()
            else: await i.delete_original_response()
        except (discord.HTTPException,discord.NotFound,discord.Forbidden,ValueError,AttributeError): pass
        return
    if source_message_id is not None:
        ok=await edit_source_message(i,source_message_id,embed=e,view=PaymentMethodView(oid))
        if not ok:
            await i.followup.send(embed=e,view=PaymentMethodView(oid),ephemeral=True)
        return
    await i.edit_original_response(embed=e,view=PaymentMethodView(oid))

async def create_boost_order(i,tier):
    if shop_paused(i.guild.id): return await i.response.send_message(paused_message(i.guild.id),ephemeral=True)
    _fn=foreign_ticket_notice(i,i.channel)
    if _fn: return await i.response.send_message(_fn,ephemeral=True)
    price_v=boost_price(i.guild.id,tier)
    if price_v<=0: return await i.response.send_message('❌ 此代肝額度目前尚未設定價格，請聯絡店長。',ephemeral=True)
    await i.response.defer()
    return await create_order_and_show_payment(i,tier,1,price_v,price_v,'',already_deferred=True,service_type='代肝')

async def create_negotiation_order(i,p):
    if shop_paused(i.guild.id): return await i.response.send_message(paused_message(i.guild.id),ephemeral=True)
    _fn=foreign_ticket_notice(i,i.channel)
    if _fn: return await i.response.send_message(_fn,ephemeral=True)
    await i.response.defer()
    ch=i.channel; no=ticket_no(ch.name)
    existing=q("SELECT * FROM orders WHERE channel_id=? AND buyer_id=? AND status NOT IN ('結單','已取消') ORDER BY id DESC LIMIT 1",(str(ch.id),str(i.user.id)),True)
    if existing: return await i.followup.send(f'ℹ️ 此工單已有進行中的訂單 #{existing[0]["ticket_no"]}。',ephemeral=True)
    c=db.cursor(); c.execute('INSERT INTO orders(ticket_no,channel_id,guild_id,product,quantity,unit_price,total_price,status,buyer_id,created_at,service_type) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(no,str(ch.id),str(ch.guild.id),p,1,0,0,'待洽談',str(i.user.id),now(),'幣號')); oid=c.lastrowid; db.commit()
    order=q('SELECT * FROM orders WHERE id=?',(oid,),True)[0]
    await rename_order_channel(order,ch.guild,'不死號洽談')
    await ch.send('📋 **不死號**\n已通知店長，請稍候，店長會與您洽談。')
    await post_channel_log(ch.guild,'order_log_channel_id',f'📋 **不死號洽談通知**\n工單：{ch.mention}\n客人：{i.user.mention}\n客人選擇了不死號，請前往工單洽談。')
    await status_announce(order,ch.guild)
    await i.edit_original_response(content='📋 已通知店長，請稍候，店長會與您洽談。',embed=None,view=None)

class DeliveryAcceptView(discord.ui.View):
    def __init__(self,p,qty,unit,total,delivery):
        super().__init__(timeout=300); self.p=p; self.qty=qty; self.unit=unit; self.total=total; self.delivery=delivery
    @discord.ui.button(label='✅ 可以，接受時間',style=discord.ButtonStyle.success)
    async def accept(self,i,button):
        if price(self.p)<=0: return await i.response.send_message('❌ 商品價格已變更，請重新選購。',ephemeral=True)
        current_delivery=get(i.guild.id,'delivery_time','').strip()
        if not current_delivery:
            return await i.response.send_message('❌ 店長目前尚未設定統一交貨時間，請稍後再試。',ephemeral=True)
        await create_order_and_show_payment(i,self.p,self.qty,price(self.p),calc_total(price(self.p),self.qty),current_delivery)
        button.disabled=True
    @discord.ui.button(label='❌ 無法接受，取消',style=discord.ButtonStyle.danger)
    async def reject(self,i,button):
        await i.response.edit_message(content=template(i.guild.id,'cancel_text','❌ 已取消本次購買。'),embed=None,view=None)

async def has_payment_proof(ch, buyer_id, order_id=None):
    """付款確認前必須找到客人上傳的付款成功明細圖片。"""
    if not isinstance(ch,discord.TextChannel) or not buyer_id:
        return False
    # 先查資料庫已綁定的圖片，避免歷史訊息超過掃描範圍時漏判。
    if order_id is not None:
        stored=q('SELECT payment_proof_url,payment_proof_message_id FROM orders WHERE id=?',(order_id,),True)
        if stored and stored[0]['payment_proof_url']:
            return True
    image_exts=('.png','.jpg','.jpeg','.webp','.gif','.bmp','.heic','.heif')
    try:
        async for msg in ch.history(limit=500):
            if str(getattr(msg.author,'id','')) != str(buyer_id):
                continue
            for att in msg.attachments:
                ctype=(att.content_type or '').lower()
                filename=(getattr(att,'filename','') or '').lower()
                if ctype.startswith('image/') or filename.endswith(image_exts):
                    if order_id is not None:
                        q('UPDATE orders SET payment_proof_url=?,payment_proof_message_id=? WHERE id=?',
                          (att.url,str(msg.id),order_id))
                    return True
    except (discord.Forbidden,discord.HTTPException) as e:
        print(f'[PAYMENT_PROOF] history check failed｜channel={getattr(ch,"id",None)}｜error={e!r}')
    return False


SHIPPABLE_STATUS=('待確認付款','待交貨','處理中')

class OrderManageView(discord.ui.View):
    def __init__(self,oid):
        super().__init__(timeout=None); self.oid=oid; self.build()
    def build(self):
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True); status=r[0]['status'] if r else ''
        if status=='待確認付款':
            ch_id = r[0]['channel_id'] if r else ''
            ch = bot.get_channel(int(ch_id)) if str(ch_id).isdigit() else None
            proof_ready = bool(r and r[0]['payment_proof_url'])
            # 建立 View 時先看資料庫；若圖片尚未綁定，再嘗試掃描目前工單。
            if not proof_ready and isinstance(ch,discord.TextChannel) and r:
                # build() 不能 await；先不顯示確認按鈕，等 on_message 收到圖片後重新發送可確認的 View。
                proof_ready = False
            if proof_ready:
                b=discord.ui.Button(label='💰 確認付款',style=discord.ButtonStyle.success,custom_id=f'ct_confirm:{self.oid}',row=0); b.callback=self.confirm_payment; self.add_item(b)
        elif status=='待交貨':
            b=discord.ui.Button(label='🛠️ 開始處理',style=discord.ButtonStyle.primary,custom_id=f'ct_process:{self.oid}',row=0); b.callback=self.start; self.add_item(b)
        elif status=='處理中':
            b=discord.ui.Button(label='📦 完成交貨',style=discord.ButtonStyle.primary,custom_id=f'ct_deliver:{self.oid}',row=0); b.callback=self.deliver; self.add_item(b)
        elif status=='待收貨':
            b=discord.ui.Button(label='✅ 完成訂單',style=discord.ButtonStyle.success,custom_id=f'ct_finish:{self.oid}',row=0); b.callback=self.finish; self.add_item(b)
        # 🚚 發貨：幣號規格 + 已收到付款明細（或已確認付款）→ 一鍵從庫存發免洗帳號
        if r and r[0]['service_type']=='幣號' and r[0]['product'] in COIN_PRODUCTS and status in SHIPPABLE_STATUS and (r[0]['payment_proof_url'] or status!='待確認付款'):
            b=discord.ui.Button(label='🚚 發貨',style=discord.ButtonStyle.success,custom_id=f'ct_ship:{self.oid}',row=0); b.callback=self.ship; self.add_item(b)
        r2=q('SELECT channel_id FROM orders WHERE id=?',(self.oid,),True); url=None
        if r2:
            ch=bot.get_channel(int(r2[0]['channel_id']))
            if isinstance(ch,discord.TextChannel): url=ch.jump_url
        if url: self.add_item(discord.ui.Button(label='🎫 前往工單',style=discord.ButtonStyle.link,url=url,row=0))
    async def check(self,i): return admin(i.user)
    async def confirm_payment(self,i):
        if not await self.check(i): return await deny(i)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return await i.response.send_message('❌ 找不到訂單。',ephemeral=True)
        o=r[0]
        if o['status']!='待確認付款': return await i.response.send_message(f'ℹ️ 目前狀態為「{o["status"]}」。',ephemeral=True)
        ch=i.guild.get_channel(int(o['channel_id']))
        if not await has_payment_proof(ch,o['buyer_id'],self.oid):
            return await i.response.send_message('❌ 尚未找到客人上傳的付款成功明細圖片，請先請客人上傳圖片後再確認付款。',ephemeral=True)
        if not await confirm_dialog(i,'確認這筆付款已收到嗎？','確認付款',self.oid): return
    async def ship(self,i):
        if not await self.check(i): return await deny(i)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return await i.response.send_message('❌ 找不到訂單。',ephemeral=True)
        o=r[0]
        if o['status'] not in SHIPPABLE_STATUS:
            return await i.response.send_message(f'ℹ️ 訂單目前是「{o["status"]}」，無法發貨。',ephemeral=True)
        if o['status']=='待確認付款' and not o['payment_proof_url']:
            return await i.response.send_message('❌ 尚未收到客人的付款明細圖片。',ephemeral=True)
        done=q("SELECT COUNT(*) AS n FROM accounts WHERE order_id=? AND status='issued'",(o['id'],),True)[0]['n']
        if done>=int(o['quantity']):
            return await i.response.send_message(f'❌ 這張訂單已發過 **{done}** 組帳號，不能重複發貨。要補發請用 `/發號`。',ephemeral=True)
        qty=int(o['quantity'])
        if qty>100:
            return await i.response.send_message(f'❌ 這張訂單是 **{qty}** 隻，超過單次發號上限 100 組，「🚚 發貨」無法一次發完。請用 `/發號` 分批發（每次最多 100 組）。',ephemeral=True)
        await i.response.defer(ephemeral=True)
        ch=i.guild.get_channel(int(o['channel_id']))
        if not isinstance(ch,discord.TextChannel):
            return await i.followup.send('❌ 找不到這張訂單的工單頻道。',ephemeral=True)
        # 隨機挑一種庫存足夠的登入方式，發客人買的規格與數量。
        stock_by={lt:q("SELECT COUNT(*) AS n FROM accounts WHERE guild_id=? AND product=? AND login_type=? AND status='available'",(str(i.guild.id),o['product'],lt),True)[0]['n'] for lt in POOL_TYPES}
        ok_types=[lt for lt,n in stock_by.items() if n>=qty]
        if not ok_types:
            detail='、'.join(f'{_lt_label(lt)} {n} 組' for lt,n in stock_by.items() if n or lt!=LOGIN_NONE)
            return await i.followup.send(f'❌ 庫存不足：{o["product"]} 需要 **{qty}** 組，但單一登入方式都不夠（{detail}）。請先 `/上架帳號` 補貨，或用 `/發號` 手動分批發。',ephemeral=True)
        lt=random.choice(ok_types)
        _ok,msg=await issue_accounts_core(i.guild,ch,o,lt,qty,o['product'],i.user,force_status=True,force_dup=False,qty_given=False)
        await i.followup.send(msg,ephemeral=True)
    async def start(self,i): await self.change(i,'處理中','開始處理',expect='待交貨')
    async def deliver(self,i): await self.change(i,'待收貨','完成交貨',expect='處理中')
    async def change(self,i,new_status,action,expect=None):
        if not await self.check(i): return await deny(i)
        await i.response.defer(ephemeral=True)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return await i.followup.send('❌ 找不到訂單。',ephemeral=True)
        o=r[0]
        if expect and o['status']!=expect:
            return await i.followup.send(f'ℹ️ 訂單 #{o["ticket_no"]} 目前是「{o["status"]}」，這個按鈕已過期，請看最新的訂單卡。',ephemeral=True)
        q('UPDATE orders SET status=? WHERE id=?',(new_status,self.oid)); log_order(self.oid,i.guild.id,i.user.id,action,f'{o["status"]} → {new_status}')
        await rename_order_channel(q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0],i.guild,action)
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await status_announce(rr,i.guild); await i.followup.send(f'✅ 訂單 #{o["ticket_no"]} 已更新為 **{new_status}**。',ephemeral=True)
    async def finish(self,i):
        if not await self.check(i): return await deny(i)
        await i.response.defer(ephemeral=True)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return await i.followup.send('❌ 找不到訂單。',ephemeral=True)
        o=r[0]
        if o['status']!='待收貨': return await i.followup.send(f'ℹ️ 目前狀態為「{o["status"]}」，無法直接完成訂單。',ephemeral=True)
        q('UPDATE orders SET status=?,completed_at=? WHERE id=?',('結單',now(),self.oid)); log_order(self.oid,i.guild.id,i.user.id,'完成訂單','管理員完成訂單')
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]
        await rename_order_channel(rr,i.guild,'完成訂單')
        ch=i.guild.get_channel(int(rr['channel_id']))
        if isinstance(ch,discord.TextChannel):
            await move_closed_ticket_to_service_category(ch,rr['service_type'] if 'service_type' in rr.keys() else '幣號')
        await status_announce(rr,i.guild); await i.followup.send(f'✅ 訂單 #{o["ticket_no"]} 已完成並結單（客人權限保留）。',ephemeral=True)

class ConfirmView(discord.ui.View):
    def __init__(self,oid): super().__init__(timeout=60); self.oid=oid
    @discord.ui.button(label='✅ 確認',style=discord.ButtonStyle.success)
    async def yes(self,i,b):
        if not admin(i.user): return await deny(i)
        await i.response.defer(ephemeral=True)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return await i.followup.send('❌ 找不到訂單。',ephemeral=True)
        o=r[0]
        if o['status']!='待確認付款': return await i.followup.send('ℹ️ 這筆訂單已不是待確認付款。',ephemeral=True)
        ch=i.guild.get_channel(int(o['channel_id']))
        if not await has_payment_proof(ch,o['buyer_id'],self.oid):
            return await i.followup.send('❌ 尚未找到客人上傳的付款成功明細圖片，請先請客人上傳圖片後再確認付款。',ephemeral=True)
        new='待排單' if o['service_type']=='代肝' else '待交貨'
        q('UPDATE orders SET status=? WHERE id=?',(new,self.oid)); log_order(self.oid,i.guild.id,i.user.id,'確認付款',f'{o["status"]} → {new}')
        ch=i.guild.get_channel(int(o['channel_id']))
        if isinstance(ch,discord.TextChannel) and o['service_type']=='代肝':
            await ch.send('🎮 **請提供遊戲帳號**\n付款已確認，請直接在此工單提供您的遊戲帳號。\n📢 已通知店長，收到帳號後將安排排單。')
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await rename_order_channel(rr,i.guild,'店長確認付款後自動更新'); await status_announce(rr,i.guild)
        await i.edit_original_response(content=f'✅ 已確認付款｜#{o["ticket_no"]}',view=None)
    @discord.ui.button(label='取消',style=discord.ButtonStyle.secondary)
    async def no(self,i,b): await i.response.edit_message(content='已取消確認操作。',view=None)

async def confirm_dialog(i,text,title,oid):
    if not admin(i.user): return await deny(i)
    await i.response.send_message(f'💰 **{title}**\n{text}',view=ConfirmView(oid),ephemeral=True); return True

class BalanceConfirmView(discord.ui.View):
    """客人按「使用餘額付款」後的二次確認（避免手滑扣款）。"""
    def __init__(self,oid,panel=None): super().__init__(timeout=60); self.oid=oid; self.panel=panel
    @discord.ui.button(label='✅ 確認扣款',style=discord.ButtonStyle.success)
    async def yes(self,i,b):
        await i.response.defer(ephemeral=True)
        ok,msg,o=pay_order_with_balance(self.oid,i.user.id)
        if not ok: return await i.edit_original_response(content=msg,view=None)
        log_order(self.oid,o['guild_id'],i.user.id,'餘額付款',f'NT${int(o["balance_paid"]):,}｜餘額付清，狀態 → {o["status"]}')
        await i.edit_original_response(content=f'{msg}\n訂單 #{o["ticket_no"]} 已進入「{o["status"]}」，請等候店長處理。',view=None)
        # 把公開的付款面板收掉，避免客人再按別的付款方式。
        if self.panel is not None:
            try:
                e=discord.Embed(title='💰 已使用餘額付款',description=f'訂單 #{o["ticket_no"]}｜扣款 **NT${int(o["balance_paid"]):,}**\n付款完成，請等候店長處理。',color=UI_COLOR['ok'])
                await self.panel.edit(embed=e,view=None)
            except discord.HTTPException: pass
        try:
            await rename_order_channel(o,i.guild,'客人用餘額付款後自動更新'); await status_announce(o,i.guild)
        except Exception: traceback.print_exc()
        sup=support_role(i.guild); ping=(sup.mention+' ') if sup is not None else ''
        am=discord.AllowedMentions(roles=([sup] if sup is not None else False),users=True,everyone=False)
        if isinstance(i.channel,discord.TextChannel):
            try:
                await i.channel.send(f'{ping}💰 **餘額付款完成**\n{i.user.mention} 已用餘額付清訂單 #{o["ticket_no"]}（NT${int(o["balance_paid"]):,}），系統已自動確認付款，可直接處理。',allowed_mentions=am)
                if o['service_type']=='代肝':
                    await i.channel.send('🎮 **請提供遊戲帳號**\n付款已確認，請直接在此工單提供您的遊戲帳號。\n📢 已通知店長，收到帳號後將安排排單。')
            except discord.HTTPException: pass
        await post_channel_log(i.guild,'order_log_channel_id',f'💰 **餘額付款**｜#{o["ticket_no"]}｜{o["product"]} × {o["quantity"]}｜NT${int(o["balance_paid"]):,}｜{i.user.mention}')
    @discord.ui.button(label='取消',style=discord.ButtonStyle.secondary)
    async def no(self,i,b): await i.response.edit_message(content='已取消，沒有扣款。',view=None)

class PaymentMethodView(discord.ui.View):
    def __init__(self,oid):
        super().__init__(timeout=None); self.oid=oid
        # 每張訂單使用獨立 custom_id，避免多張付款面板互相吃到別張訂單的 callback。
        b=discord.ui.Button(label='🏪 無卡存款（帶紙鈔至 7-11）',style=discord.ButtonStyle.primary,custom_id=f'ct_pay_nocard:{oid}',row=0)
        b.callback=self.nocard; self.add_item(b)
        b2=discord.ui.Button(label='🏦 匯款（轉帳）',style=discord.ButtonStyle.primary,custom_id=f'ct_pay_transfer:{oid}',row=0)
        b2.callback=self.transfer; self.add_item(b2)
        b4=discord.ui.Button(label='💰 使用餘額付款',style=discord.ButtonStyle.success,custom_id=f'ct_pay_balance:{oid}',row=1)
        b4.callback=self.balance_pay; self.add_item(b4)
        b3=discord.ui.Button(label='🔄 重新選擇',style=discord.ButtonStyle.secondary,custom_id=f'ct_reset_m:{oid}',row=1)
        b3.callback=self.reset; self.add_item(b3)
    async def reset(self,i): await reset_pending_order(i,self.oid)
    async def balance_pay(self,i):
        await i.response.defer(ephemeral=True)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r or str(r[0]['buyer_id'])!=str(i.user.id): return await i.followup.send('❌ 這不是您的訂單，無法操作。',ephemeral=True)
        o=r[0]
        if o['status']!='待付款': return await i.followup.send(f'ℹ️ 這筆訂單目前是「{o["status"]}」，不能再用餘額付款。',ephemeral=True)
        total=int(o['total_price'] or 0); bal=_balance_of(i.user.id)
        if bal<=0: return await i.followup.send('💰 您目前沒有餘額，請改用「無卡存款」或「匯款（轉帳）」。',ephemeral=True)
        if bal<total:
            return await i.followup.send(f'💰 **餘額不足**\n目前餘額 **NT${bal:,}**，本單需要 **NT${total:,}**（還差 NT${total-bal:,}）。\n餘額不能與其他付款方式合併，請改用「無卡存款」或「匯款（轉帳）」，或請店長幫您加值後再來。',ephemeral=True)
        await i.followup.send(f'💰 **確認使用餘額付款？**\n訂單 #{o["ticket_no"]}｜{o["product"]} × {o["quantity"]}\n扣款 **NT${total:,}**，付款後餘額 **NT${bal-total:,}**。',view=BalanceConfirmView(self.oid,i.message),ephemeral=True)
    async def choose(self,i,method):
        # 先確認 Interaction，再進行資料庫、狀態頻道與付款頁更新，避免超過 Discord 3 秒限制。
        try:
            await i.response.defer()
        except discord.InteractionResponded:
            pass
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r or str(r[0]['buyer_id'])!=str(i.user.id): return await i.followup.send('❌ 這不是您的訂單，無法操作。',ephemeral=True)
        o=r[0]
        if o['status']!='待付款': return await i.followup.send('ℹ️ 這筆訂單目前無法選擇付款方式。',ephemeral=True)
        q('UPDATE orders SET payment_method=? WHERE id=?',(method,self.oid)); log_order(self.oid,o['guild_id'],i.user.id,'選擇付款方式',method)
        if method=='無卡存款':
            text=template(i.guild.id,'no_card_text','中信（7-11ATM）\n銀行帳戶：')
            if '{bank_info}' in text:
                text=text.replace('{bank_info}',payment_info_md(i.guild.id))
        else:
            text=safe_format(template(i.guild.id,'transfer_text','🏦 **匯款（轉帳）**\n請依下方資訊完成轉帳：\n\n{bank_info}'),bank_info=payment_info_md(i.guild.id))
        text=f'{text}\n\n# 💰 應付金額：NT${int(o["total_price"]):,}'
        e=discord.Embed(title=f'💳 {method}',description=text,color=UI_COLOR['money'])
        e.set_footer(text=template(i.guild.id,'payment_selected_text','完成付款後，請按下「💳 完成付款」通知店家。'))
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]
        await status_announce(rr,i.guild)
        await i.edit_original_response(embed=e,view=PaymentView(self.oid))
    async def nocard(self,i): await self.choose(i,'無卡存款')
    async def transfer(self,i): await self.choose(i,'匯款（轉帳）')

class PaymentView(discord.ui.View):
    def __init__(self,oid):
        super().__init__(timeout=None); self.oid=oid
        b=discord.ui.Button(label='💳 完成付款',style=discord.ButtonStyle.success,custom_id=f'ct_paid:{oid}'); b.callback=self.paid; self.add_item(b)
        b2=discord.ui.Button(label='❌ 取消訂單',style=discord.ButtonStyle.danger,custom_id=f'ct_cancel:{oid}'); b2.callback=self.cancel; self.add_item(b2)
        b3=discord.ui.Button(label='🔄 重新選擇',style=discord.ButtonStyle.secondary,custom_id=f'ct_reset:{oid}'); b3.callback=self.reset; self.add_item(b3)
    async def reset(self,i): await reset_pending_order(i,self.oid)
    async def paid(self,i):
        try: await i.response.defer()
        except discord.InteractionResponded: pass
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r or str(r[0]['buyer_id'])!=str(i.user.id): return await i.followup.send('❌ 這不是您的訂單，無法操作。',ephemeral=True)
        o=r[0]
        if o['status']!='待付款': return await i.followup.send('ℹ️ 這筆訂單已經提交過付款通知，請勿重複操作。',ephemeral=True)
        q('UPDATE orders SET status=?,paid_at=? WHERE id=?',('待確認付款',now(),self.oid)); log_order(self.oid,o['guild_id'],i.user.id,'完成付款',f'付款方式：{o["payment_method"] or "未選擇"}')
        guild=i.guild; updated=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]
        # 支援「先上傳付款圖片、再按完成付款」：狀態切成待確認付款後立即回掃歷史並綁定圖片。
        proof_found=await has_payment_proof(i.channel,o['buyer_id'],self.oid)
        updated=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]
        await rename_order_channel(updated,guild,'客人完成付款後自動更新工單名稱'); await status_announce(updated,guild)
        sup=support_role(guild)   # 付款通知要 @客服身分組
        ping=(sup.mention+' ') if sup is not None else ''
        am=discord.AllowedMentions(roles=([sup] if sup is not None else False),users=True,everyone=False)
        if proof_found:
            await i.channel.send(f'{ping}💳 **付款通知**\n已收到 {i.user.mention} 的付款通知。\n\n🧾 **已找到付款成功明細圖片**，已綁定至訂單 #{updated["ticket_no"]}。\n⏳ 請稍候，店長確認後會通知您。', allowed_mentions=am)
        else:
            await i.channel.send(f'{ping}💳 **付款通知**\n已收到 {i.user.mention} 的付款通知。\n\n🧾 請在此工單上傳**付款成功明細圖片**。\n如果沒有附上圖片，店長會提醒您補上。', allowed_mentions=am)
        await i.followup.send(template(i.guild.id,'paid_text','✅ **成功提交付款通知，請等待店長**'),ephemeral=True)
    async def cancel(self,i):
        try: await i.response.defer()
        except discord.InteractionResponded: pass
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r or str(r[0]['buyer_id'])!=str(i.user.id): return await i.followup.send('❌ 這不是您的訂單，無法操作。',ephemeral=True)
        if r[0]['status']!='待付款': return await i.followup.send('❌ 這筆訂單已進入付款流程，目前無法取消。',ephemeral=True)
        with db:
            cur=db.cursor(); cur.execute('UPDATE orders SET status=? WHERE id=? AND status=?',('已取消',self.oid,'待付款'))
            cur.execute('SELECT product,quantity,stock_reserved FROM orders WHERE id=?',(self.oid,)); rr=cur.fetchone()
            if rr and int(rr['stock_reserved'] or 0)==1:
                cur.execute('UPDATE products SET stock=stock+? WHERE product=?',(int(rr['quantity']),rr['product']))
                cur.execute('UPDATE orders SET stock_reserved=0 WHERE id=?',(self.oid,))
            db.commit()
        updated=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await rename_order_channel(updated,i.guild,'客人取消訂單'); await i.followup.send('❌ 訂單已取消。'); await status_announce(updated,i.guild)


@bot.tree.command(name='設定代肝價格',description='設定代肝額度價格')
@app_commands.describe(額度='例如 300M',價格='價格（NT）')
@app_commands.choices(額度=choices(BOOST_TIERS))
async def set_boost_price(i,額度:str,價格:int):
    if not strict_admin(i.user): return await deny(i, True)
    if 價格<0: return await i.response.send_message('❌ 價格不能小於 0。',ephemeral=True)
    q('INSERT INTO boost_prices(guild_id,tier,price) VALUES(?,?,?) ON CONFLICT(guild_id,tier) DO UPDATE SET price=excluded.price',(str(i.guild.id),額度,價格)); await i.response.send_message(f'✅ {額度} 代肝價格已設定為 NT${價格:,}。',ephemeral=True)

@bot.tree.command(name='代肝價格表',description='查看代肝價格')
async def boost_prices(i):
    if not admin(i.user): return await deny(i)
    lines=[]
    for t in BOOST_TIERS:
        v=boost_price(i.guild.id,t)
        lines.append(f'▸ **{t}**　NT${v:,}' if v>0 else f'▸ **{t}**　尚未設定')
    e=discord.Embed(title='🛠️ 代肝價格',description='\n'.join(lines),color=UI_COLOR['info'])
    brand_footer(e)
    await i.response.send_message(embed=e,ephemeral=True)

@bot.tree.command(name='設定幣號狀態',description='設定幣號規格是否正常提供')
@app_commands.describe(商品='幣號規格',狀態='正常提供／暫停提供／缺貨')
@app_commands.choices(商品=choices(SELLABLE_PRODUCTS),狀態=choices(AVAILABILITY))
async def set_coin_status(i,商品:str,狀態:str):
    if not admin(i.user): return await deny(i)
    q('INSERT OR IGNORE INTO products(product,price,stock,enabled,availability_status) VALUES(?,?,?,?,?)',(商品,0,0,1,狀態)); q('UPDATE products SET availability_status=? WHERE product=?',(狀態,商品)); await i.response.send_message(f'✅ {商品} 狀態已設定為 **{狀態}**。',ephemeral=True)

@bot.tree.command(name='設定價目表頻道',description='設定幣號與代肝價目表頻道')
@app_commands.describe(類型='價目表類型',頻道='頻道')
@app_commands.choices(類型=choices(['幣號價目表','代肝價目表']))
async def set_price_channel(i,類型:str,頻道:discord.TextChannel):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'coin_price_channel_id' if 類型=='幣號價目表' else 'boost_price_channel_id',頻道.id); await i.response.send_message(f'✅ {類型}已設定為 {頻道.mention}',ephemeral=True)

@bot.tree.command(name='設定非營業時間',description='開啟非營業時間模式')
async def set_off_hours(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'off_hours','1'); await i.response.send_message('🕐 已開啟非營業時間。客人仍可正常下單及付款，付款後等待店長回來確認。',ephemeral=True)

@bot.tree.command(name='關閉非營業時間',description='關閉非營業時間模式')
async def close_off_hours(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'off_hours','0'); await i.response.send_message('🟢 已關閉非營業時間模式。',ephemeral=True)

@bot.tree.command(name='關閉自動算價',description='關閉聊天室輸入「100M不死*數量／100M普通*數量／代肝額度」時的自動算價')
async def disable_auto_price(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'auto_price_calc','0')
    await i.response.send_message('⏸️ 已關閉聊天室自動算價。之後輸入 100M不死*3、200M 這類內容，機器人不會自動回覆價格。',ephemeral=True)

@bot.tree.command(name='開啟自動算價',description='開啟聊天室輸入「100M不死*數量／100M普通*數量／代肝額度」時的自動算價')
async def enable_auto_price(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'auto_price_calc','1')
    await i.response.send_message('▶️ 已開啟聊天室自動算價。',ephemeral=True)

# ---- 管理指令 ----
async def deny(i, strict=False):
    text=STRICT_DENY_TEXT if strict else ADMIN_DENY_TEXT
    if i.response.is_done():
        return await i.followup.send(text,ephemeral=True)
    return await i.response.send_message(text,ephemeral=True)

@bot.tree.command(name='價格表',description='查看目前商品價格')
async def prices(i):
    if not admin(i.user): return await deny(i)
    rows=q("SELECT product,price FROM products ORDER BY CASE product WHEN '50M' THEN 1 WHEN '100M' THEN 2 ELSE 3 END",(),True)
    lines=[]
    for r in rows:
        unit='發' if r['product']==BULLET_PRODUCT else '隻'
        lines.append(f'▸ **{r["product"]}**　NT${fmt_price(r["price"])} ／ {unit}' if float(r['price'] or 0)>0 else f'▸ **{r["product"]}**　尚未設定')
    e=discord.Embed(title='💰 目前價格',description='\n'.join(lines) or '尚無商品',color=UI_COLOR['money'])
    brand_footer(e)
    await i.response.send_message(embed=e,ephemeral=True)

@bot.tree.command(name='設定價格',description='設定商品單價')
@app_commands.describe(商品='商品',價格='單價（NT，幣號每隻／子彈每發；A大子彈可填小數，例如 0.5）')
@app_commands.choices(商品=choices(SELLABLE_PRODUCTS))
async def set_price(i,商品:str,價格:float):
    if not strict_admin(i.user): return await deny(i, True)
    if 價格!=價格 or 價格 in (float('inf'),float('-inf')): return await i.response.send_message('❌ 價格格式錯誤。',ephemeral=True)
    if 價格<0: return await i.response.send_message('❌ 價格不能小於 0。',ephemeral=True)
    價格=round(價格,4)
    if 商品!=BULLET_PRODUCT and 價格!=int(價格):
        return await i.response.send_message('❌ 只有「A大子彈」可以設定小數點價格，幣號請輸入整數。',ephemeral=True)
    if 價格==int(價格): 價格=int(價格)
    q('UPDATE products SET price=? WHERE product=?',(價格,商品)); await i.response.send_message(f'✅ {商品} 單價已設定為 NT${fmt_price(價格)}。'+('（總價會四捨五入到整數元）' if 商品==BULLET_PRODUCT and 價格!=int(價格) else ''),ephemeral=True)

@bot.tree.command(name='設定最低購買數量',description='設定單一商品的最低購買數量（幣號規格或 A大子彈）')
@app_commands.describe(商品='商品',數量='最低購買數量（隻／發），0 代表不限')
@app_commands.choices(商品=choices(SELLABLE_PRODUCTS))
async def set_min_purchase_qty(i,商品:str,數量:int):
    if not admin(i.user): return await deny(i)
    if 數量<0 or 數量>999999: return await i.response.send_message('❌ 最低購買數量請輸入 0～999999。',ephemeral=True)
    setv(i.guild.id,f'min_purchase_qty:{商品}',數量)
    await i.response.send_message(f'✅ {商品} 最低購買數量已設定為 **{數量} {"發" if 商品==BULLET_PRODUCT else "隻"}**。'+('（不限）' if 數量==0 else ''),ephemeral=True)

@bot.tree.command(name='設定庫存',description='設定目前現貨數量')
@app_commands.describe(商品='商品',數量='目前現貨隻數')
@app_commands.choices(商品=choices(SELLABLE_PRODUCTS))
async def set_stock(i,商品:str,數量:int):
    if not admin(i.user): return await deny(i)
    if 數量<0: return await i.response.send_message('❌ 庫存不能小於 0。',ephemeral=True)
    q('UPDATE products SET stock=? WHERE product=?',(數量,商品)); await i.response.send_message(f'📦 {商品} 現貨已設定為 **{數量} 隻**。',ephemeral=True)

@bot.tree.command(name='庫存',description='查看目前現貨')
async def stocks(i):
    if not admin(i.user): return await deny(i)
    ico={'正常提供':'🟢','暫停提供':'🟡','缺貨':'🔴'}
    rows=[]
    for p in SELLABLE_PRODUCTS:
        unit='發' if p==BULLET_PRODUCT else '隻'
        qty='不限庫存' if p==BULLET_PRODUCT else f'**{stock(p)}** {unit}'
        rows.append(f'{ico.get(availability(p),"🟢")} **{p}**　{qty}')
    e=discord.Embed(title='📦 目前現貨',description='\n'.join(rows),color=UI_COLOR['info'],timestamp=datetime.now(timezone.utc))
    e.set_footer(text='🟢 正常提供｜🟡 暫停提供｜🔴 缺貨')
    await i.response.send_message(embed=e,ephemeral=True)

@bot.tree.command(name='設定補貨',description='設定商品下一次補貨時間')
@app_commands.describe(商品='商品',時間='例如：9/6 20:00')
@app_commands.choices(商品=choices(SELLABLE_PRODUCTS))
async def set_restock(i,商品:str,時間:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,f'restock_{商品}',時間); await i.response.send_message(f'📦 {商品} 下一次補貨時間已設定：**{時間}**',ephemeral=True)

@bot.tree.command(name='店長狀態',description='設定店長營業或休息')
@app_commands.describe(狀態='營業中／休息中')
@app_commands.choices(狀態=choices(['營業中','休息中']))
async def shop_status(i,狀態:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'shop_status',狀態); await i.response.send_message(f'✅ 店長狀態：**{狀態}**。\n（這只是狀態標示，不會擋下單；要擋下單請用 `/暫停接單`。）',ephemeral=True)

@bot.tree.command(name='暫停接單',description='暫停客人下單')
async def pause(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'shop_status','暫停接單'); await i.response.send_message('⏸️ 已暫停接單。客人仍可開單，但無法建立新訂單（已建立的訂單不受影響）。可用 `/設定店長公告` 加上說明。',ephemeral=True)

@bot.tree.command(name='恢復接單',description='恢復客人下單')
async def resume(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'shop_status','營業中'); await i.response.send_message('▶️ 已恢復接單。',ephemeral=True)

@bot.tree.command(name='付款資訊',description='查看目前付款資訊')
async def payinfo(i):
    if not admin(i.user): return await deny(i)
    e=discord.Embed(title='💳 目前付款資訊',description=payment_info(i.guild.id),color=UI_COLOR['money']); brand_footer(e)
    await i.response.send_message(embed=e,ephemeral=True)

@bot.tree.command(name='設定付款',description='設定付款資訊')
@app_commands.describe(銀行='銀行名稱',代碼='銀行代碼',帳號='收款帳號',戶名='戶名')
async def setpay(i,銀行:str='',代碼:str='',帳號:str='',戶名:str=''):
    if not strict_admin(i.user): return await deny(i, True)
    # 只更新有填的欄位；沒填的保留原值（以前只填一欄會把其他欄位清空）。
    upd=[(k,v) for k,v in [('pay_bank',銀行),('pay_code',代碼),('pay_account',帳號),('pay_name',戶名)] if v!='']
    if not upd: return await i.response.send_message('❌ 請至少填一個欄位。',ephemeral=True)
    for k,v in upd: setv(i.guild.id,k,v)
    await i.response.send_message('✅ 付款資訊已更新。\n'+payment_info(i.guild.id),ephemeral=True)

@bot.tree.command(name='設定交貨時間',description='設定本店今日統一交貨時間')
@app_commands.describe(時間='例如：9/6 21:30')
async def set_delivery(i,時間:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'delivery_time',時間); await i.response.send_message(f'🚚 本店今日統一交貨時間已設定為：**{時間}**',ephemeral=True)

@bot.tree.command(name='設定付款提醒',description='設定待付款訂單多久後提醒一次')
@app_commands.describe(分鐘='例如 15，填 0 可關閉自動提醒')
async def set_payment_reminder(i,分鐘:int):
    if not admin(i.user): return await deny(i)
    if 分鐘<0 or 分鐘>10080: return await i.response.send_message('❌ 分鐘請輸入 0～10080。',ephemeral=True)
    setv(i.guild.id,'payment_reminder_minutes',分鐘)
    await i.response.send_message('🔔 已關閉付款提醒。' if 分鐘==0 else f'🔔 已設定待付款 **{分鐘} 分鐘**後提醒一次。',ephemeral=True)

@bot.tree.command(name='設定自動取消',description='設定待付款訂單超過多久自動取消並退回庫存')
@app_commands.describe(分鐘='例如 60；填 0 關閉自動取消（預設 60）')
async def set_auto_cancel(i,分鐘:int):
    if not admin(i.user): return await deny(i)
    if 分鐘<0 or 分鐘>10080: return await i.response.send_message('❌ 分鐘請輸入 0～10080。',ephemeral=True)
    setv(i.guild.id,'auto_cancel_minutes',分鐘)
    await i.response.send_message('🕒 已關閉自動取消，待付款訂單會一直保留庫存直到手動處理。' if 分鐘==0 else f'🕒 待付款超過 **{分鐘} 分鐘**會自動取消並退回預扣的庫存。',ephemeral=True)

@bot.tree.command(name='設定回覆時間',description='設定非營業時間時，顯示給客人看的預計回覆時間（留空清除）')
@app_commands.describe(時間='例如：今天 14:00、明天中午 12:00')
async def set_back_time(i,時間:str=''):
    if not admin(i.user): return await deny(i)
    t=時間.strip()
    if len(t)>60: return await i.response.send_message('❌ 最多 60 字。',ephemeral=True)
    setv(i.guild.id,'off_hours_back',t)
    await i.response.send_message(f'🕐 已設定預計回覆時間：**{t}**（只有「非營業時間」開啟時會顯示給客人）。' if t else '🗑️ 已清除預計回覆時間。',ephemeral=True)

@bot.tree.command(name='設定常見問題',description='設定購買面板「❓ 常見問題」按鈕顯示的內容（留空清除）')
@app_commands.describe(內容='可換行用 \\n；最多 1500 字')
async def set_faq(i,內容:str=''):
    if not admin(i.user): return await deny(i)
    t=內容.replace('\\n','\n').strip()
    if len(t)>1500: return await i.response.send_message('❌ 最多 1500 字。',ephemeral=True)
    setv(i.guild.id,'faq_text',t)
    if not t: return await i.response.send_message('🗑️ 已清除常見問題。',ephemeral=True)
    e=discord.Embed(title='❓ 常見問題（預覽）',description=t,color=UI_COLOR['info'])
    await i.response.send_message('✅ 已設定。新發的購買面板才會有「❓ 常見問題」按鈕，舊面板不會有。',embed=e,ephemeral=True)

@bot.tree.command(name='設定付款方式',description='設定付款方式頁面與各付款方式說明')
@app_commands.describe(類型='要修改的付款訊息',內容='新的訊息內容')
@app_commands.choices(類型=choices(['付款方式選擇','無卡存款說明','匯款說明','選擇付款後提示','完成付款提示']))
async def set_payment_method(i,類型:str,內容:str):
    if not strict_admin(i.user): return await deny(i, True)
    mp={'付款方式選擇':'pay_method_intro','無卡存款說明':'no_card_text','匯款說明':'transfer_text','選擇付款後提示':'payment_selected_text','完成付款提示':'paid_text'}
    setv(i.guild.id,mp[類型],內容); await i.response.send_message(f'✅「{類型}」已更新。',ephemeral=True)

@bot.tree.command(name='設定訂單紀錄',description='選擇訂單紀錄頻道')
@app_commands.describe(頻道='請選擇頻道')
async def set_order_log(i,頻道:discord.TextChannel):
    if not strict_admin(i.user): return await deny(i, True)
    setv(i.guild.id,'order_log_channel_id',頻道.id); await i.response.send_message(f'✅ 訂單紀錄頻道已設定為 {頻道.mention}',ephemeral=True)

@bot.tree.command(name='設定操作紀錄',description='選擇管理員操作紀錄頻道')
@app_commands.describe(頻道='請選擇頻道')
async def set_action_log(i,頻道:discord.TextChannel):
    if not strict_admin(i.user): return await deny(i, True)
    setv(i.guild.id,'action_log_channel_id',頻道.id); await i.response.send_message(f'✅ 操作紀錄頻道已設定為 {頻道.mention}',ephemeral=True)

@bot.tree.command(name='設定狀態頻道',description='選擇工單狀態公告頻道')
@app_commands.describe(頻道='請選擇頻道')
async def set_status_channel(i,頻道:discord.TextChannel):
    if not strict_admin(i.user): return await deny(i, True)
    setv(i.guild.id,'status_channel_id',頻道.id); await i.response.send_message(f'✅ 工單狀態公告頻道已設定為 {頻道.mention}',ephemeral=True)

@bot.tree.command(name='設定訊息',description='設定客人看到的 Bot 訊息')
@app_commands.describe(類型='要修改的訊息',內容='新的訊息內容')
@app_commands.choices(類型=choices(['沒貨提示','數量不足提示','付款完成提示','取消提示','暫停接單提示','狀態公告格式']))
async def setmsg(i,類型:str,內容:str):
    if not admin(i.user): return await deny(i)
    mp={'沒貨提示':'out_of_stock','數量不足提示':'not_enough_stock','付款完成提示':'paid_text','取消提示':'cancel_text','暫停接單提示':'paused_text','狀態公告格式':'status_template'}
    setv(i.guild.id,mp[類型],內容); await i.response.send_message(f'✅「{類型}」已更新。',ephemeral=True)


# ===== 分類／發號／排序輔助函式 =====
async def _safe_edit_channel(ch, timeout=20, **kwargs):
    """channel.edit 加上逾時。Discord 對頻道改名有頻率限制（約 10 分鐘 2 次），
    被限速時 discord.py 會一直等待，讓指令卡死；這裡逾時就放棄並回傳 False。"""
    try:
        await asyncio.wait_for(ch.edit(**kwargs), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        print(f'[CHANNEL_EDIT] 逾時（可能被 Discord 限速）｜channel={getattr(ch,"id",None)}｜fields={list(kwargs)}')
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f'[CHANNEL_EDIT] 失敗｜channel={getattr(ch,"id",None)}｜fields={list(kwargs)}｜error={e!r}')
    return False

def _live_max_ticket_no(guild):
    """目前伺服器內實際存在的工單頻道中，最大的工單號（沒有回傳 0）。"""
    top = 0
    if guild is None: return 0
    for ch in guild.text_channels:
        try:
            if not is_ticket_candidate(ch): continue
            no = _resolve_ticket_number(ch)
        except Exception:
            continue
        if no is not None and str(no).isdigit(): top = max(top, int(no))
    return top

async def allocate_ticket_no(guild_id, guild=None):
    """配發下一個工單號（4 位數字字串）。
    有傳入 guild 時，以「現存工單頻道的最大號 + 1」與計數器取較大者，
    已刪除頻道留下的舊號碼不會讓號碼跳號；沒傳 guild 時退回資料庫最大號。"""
    gid = str(guild_id)
    live = _live_max_ticket_no(guild) if guild is not None else None
    async with lock:
        with db:
            cur = db.cursor()
            cur.execute('SELECT next_no FROM ticket_counters WHERE guild_id=?', (gid,))
            row = cur.fetchone()
            n = int(row['next_no']) if row else 1
            if live is not None:
                n = max(n, live + 1)
            else:
                for sql in ('SELECT MAX(CAST(ticket_no AS INTEGER)) AS m FROM ticket_channels WHERE guild_id=?',
                            'SELECT MAX(CAST(ticket_no AS INTEGER)) AS m FROM orders WHERE guild_id=?'):
                    cur.execute(sql, (gid,))
                    r = cur.fetchone()
                    if r and r['m'] is not None:
                        n = max(n, int(r['m']) + 1)
            cur.execute('INSERT INTO ticket_counters(guild_id,next_no) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET next_no=excluded.next_no', (gid, n + 1))
    return f'{n:04d}'

async def release_ticket_no(guild_id, no):
    """開單失敗時退還剛配發的號碼（只有計數器仍停在 no+1 時才退，避免影響別人剛開的單）。"""
    try:
        n=int(no)
        async with lock:
            with db:
                db.execute('UPDATE ticket_counters SET next_no=? WHERE guild_id=? AND next_no=?',(n,str(guild_id),n+1))
    except Exception as e:
        print(f'[TICKET_NO] 退還工單號失敗｜no={no}｜error={e!r}')

IN_SERVICE_CATEGORY_KEYWORD = '服務中'   # 名稱含「服務中」的分類：裡面的工單只有關閉工單／完成訂單時才會被機器人移動
DEFAULT_COIN_CATEGORY_PREFIX = '幣號工單'   # 排序時，找不到其他線索就用這個名稱找幣號分類串
STAGING_PREFIX = '整理中暫存'   # 排序時暫存工單用的分類名稱前綴；整理完會自動刪除
PANEL_CLEANED = False   # 舊面板清理是否已在這次啟動做過
RENAME_CONCURRENCY = 4   # 排序改名時同時送出的請求數（限速時 discord.py 會自動排隊）
CATEGORY_CHANNEL_LIMIT = 50          # Discord 規定：一個分類最多 50 個頻道
ticket_open_lock = asyncio.Lock()    # 開單時選分類／發號／建頻道一次只跑一張，避免同時塞爆同一個分類

def _base_ticket_category(guild):
    cid = str(get(guild.id, 'ticket_category_id', '') or '')
    if cid.isdigit():
        cat = guild.get_channel(int(cid))
        # 開單分類可以是「服務中」分類（新單一開就算服務中）；排序用的幣號串不受它影響（見 ticket_category_chain 的 for_arrange）。
        if isinstance(cat, discord.CategoryChannel):
            return cat
    return None

def _split_category_name(name):
    """'幣號工單3' → ('幣號工單', 3)；'幣號工單' → ('幣號工單', 0)。"""
    m = re.match(r'^(.*?)(\d*)$', (name or '').strip())
    return m.group(1).rstrip(), (int(m.group(2)) if m.group(2) else 0)

def _closed_category_ids(guild):
    ids = set()
    for key_ in ('service_category_coin_id', 'service_category_boost_id'):
        v = str(get(guild.id, key_, '') or '')
        if v.isdigit(): ids.add(int(v))
    return ids

def ticket_category_chain(guild, fallback_channels=None, for_arrange=False):
    """依 /設定開單分類 的分類名稱，找出整串接續分類：幣號工單、幣號工單1、幣號工單2…（依數字由小到大）。
    沒設定開單分類時，若有給 fallback_channels，就從這些工單目前所在的分類名稱推斷（取最多工單的名稱前綴）。
    回傳 (名稱前綴, 分類清單)；找不到回傳 (None, [])。"""
    closed = _closed_category_ids(guild)
    base = _base_ticket_category(guild)
    base_in_service = base is not None and IN_SERVICE_CATEGORY_KEYWORD in (base.name or '')
    ignore_service_base = for_arrange and base_in_service
    if ignore_service_base:
        # 排序：開單分類只是「新單落腳的地方」，跟排序要放進哪一串分類無關。
        # 開單分類是「服務中」時，排序就當作沒有開單分類，改從工單目前所在的分類／預設名稱「幣號工單」找幣號串。
        base = None; base_in_service = False
    if base is not None:
        prefix, _ = _split_category_name(base.name)
        if not prefix:
            return prefix, [base]
    elif fallback_channels or ignore_service_base:
        counts = {}
        for ch in (fallback_channels or []):
            cat = getattr(ch, 'category', None)
            if cat is None or cat.id in closed or IN_SERVICE_CATEGORY_KEYWORD in (cat.name or ''): continue
            p, _ = _split_category_name(cat.name)
            if p: counts[p] = counts.get(p, 0) + 1
        if counts:
            prefix = max(counts, key=counts.get)
        elif ignore_service_base and any(_split_category_name(c.name)[0] == DEFAULT_COIN_CATEGORY_PREFIX and c.id not in closed
                                 and IN_SERVICE_CATEGORY_KEYWORD not in (c.name or '') for c in guild.categories):
            prefix = DEFAULT_COIN_CATEGORY_PREFIX
        else:
            return None, []
    else:
        return None, []
    chain = [c for c in guild.categories if _split_category_name(c.name)[0] == prefix and c.id not in closed and (base_in_service or IN_SERVICE_CATEGORY_KEYWORD not in c.name)]
    chain.sort(key=lambda c: (_split_category_name(c.name)[1], c.position))
    if not chain and base is not None: chain = [base]
    return (prefix, chain) if chain else (None, [])

async def _create_next_chain_category(guild, chain, prefix):
    """在整串分類的最後面再開一個：名稱 = 前綴 + (目前最大數字 + 1)，權限沿用最後一個分類。"""
    last = chain[-1]
    nxt = max(_split_category_name(c.name)[1] for c in chain) + 1
    # 新分類沿用現有分類的寫法：現有是「幣號工單 3」（有空格）就建「幣號工單 4」，沒空格就建「幣號工單4」。
    sep = ''
    for c in reversed(chain):
        m = re.match(r'^.*?(\s*)\d+$', (c.name or '').strip())
        if m and re.search(r'\d+$', (c.name or '').strip()):
            sep = m.group(1); break
    return await guild.create_category(name=f'{prefix}{sep}{nxt}', overwrites=last.overwrites,
                                       position=last.position + 1,
                                       reason='CT小舖 工單分類已滿，自動建立下一個分類')

async def pick_ticket_category(guild):
    """新工單要放的分類：整串分類的最後一個；滿 50 個就自動往下開新的一個。
    沒設定開單分類回傳 None；分類滿了又建不出新的會丟出例外。"""
    prefix, chain = ticket_category_chain(guild)
    if not chain:
        return None
    last = chain[-1]
    if len(last.channels) < CATEGORY_CHANNEL_LIMIT:
        return last
    new_cat = await _create_next_chain_category(guild, chain, prefix)
    print(f'[TICKET_CATEGORY] {last.name} 已滿，已自動建立 {new_cat.name}｜guild={guild.id}')
    return new_cat

def _ticket_service(ch):
    """工單屬於哪個服務：先看頻道名稱，再看訂單紀錄；都看不出來回傳 None。"""
    sv = _service_from_name(ch.name)
    if sv in ('幣號', '代肝', BULLET_SERVICE): return sv
    try:
        rows = q('SELECT service_type FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1', (str(ch.id),), True)
        if rows and rows[0]['service_type'] in ('幣號', '代肝', BULLET_SERVICE): return rows[0]['service_type']
    except Exception:
        pass
    return None

def boost_category_chain(guild):
    """代肝工單的整串分類：名稱含「代肝」、不是結單分類、也不是「服務中」，例如「代肝工單」「代肝工單 1」…。
    回傳 (名稱前綴, 分類清單)；沒有回傳 (None, [])。"""
    closed = _closed_category_ids(guild)
    cands = [c for c in guild.categories
             if '代肝' in (c.name or '') and c.id not in closed and IN_SERVICE_CATEGORY_KEYWORD not in (c.name or '')
             and '結單' not in (c.name or '')]
    if not cands: return None, []
    groups = {}
    for c in cands:
        groups.setdefault(_split_category_name(c.name)[0], []).append(c)
    # 多組時：偏好名稱含「工單」的，再取分類數最多的。
    prefix = max(groups, key=lambda p: ('工單' in p, len(groups[p])))
    chain = sorted(groups[prefix], key=lambda c: (_split_category_name(c.name)[1], c.position))
    return prefix, chain

async def _sweep_staging(guild, placed_ids, res, note):
    """整理完把所有「整理中暫存」分類刪掉（包含之前中斷、失敗留下來的）。
    裡面只剩已經搬走的工單（placed_ids）或是空的就刪；還有沒搬出去的工單就保留，並回報。"""
    res.setdefault('staging_deleted', 0); res.setdefault('staging_kept', 0)
    for cat in list(guild.categories):
        if not (cat.name or '').startswith(STAGING_PREFIX): continue
        left = [x for x in cat.channels if x.id not in placed_ids]
        if left:
            res['staging_kept'] += 1
            print(f'[ARRANGE] {cat.name} 內還有 {len(left)} 個頻道，保留不刪')
            continue
        try:
            await cat.delete(reason='CT小舖 整理完成，刪除暫存分類')
            res['staging_deleted'] += 1
        except Exception as e:
            print(f'[ARRANGE] 刪除暫存分類失敗｜{cat.name}｜error={e!r}'); note(e)
            res['staging_kept'] += 1

async def arrange_ticket_chain(guild, ordered_channels, include_closed=False, numbers=None, skip_ids=None):
    """把工單放進對應服務的整串分類，每個分類最多 50 張：
    幣號工單 → 幣號工單、幣號工單1、幣號工單2…；代肝工單 → 代肝工單、代肝工單1…。
    兩個服務各自依「工單號由小到大」填滿分類（號碼是全伺服器共用，所以同一服務內會有跳號，這是正常的）。
    ordered_channels 必須是「所有工單」依新工單號（小→大）排好的清單。
    服務判斷：頻道名稱 → 訂單紀錄；都判斷不出來的，原本在代肝分類就留在代肝，否則當幣號。
    include_closed=False 時，已放在「幣號結單／代肝結單」固定分類內的工單不動。
    名稱沒有工單號的頻道不會出現在 ordered_channels，完全不受影響。"""
    L = CATEGORY_CHANNEL_LIMIT
    res = {'moved': 0, 'created': 0, 'failed': 0, 'skipped': False, 'errors': [], 'layout': [], 'inferred': None}
    def note(e):
        t = f'{type(e).__name__}: {str(e)[:90]}'
        if t not in res['errors'] and len(res['errors']) < 3: res['errors'].append(t)
    me_ = guild.me
    if me_ is not None and not me_.guild_permissions.manage_channels:
        res['skipped'] = True; res['errors'].append('機器人沒有「管理頻道」權限，無法搬移頻道'); return res

    closed_ids = _closed_category_ids(guild)
    skip_ids = set(skip_ids or ())   # 服務中的工單：不搬、不排
    tickets_all = [ch for ch in ordered_channels if ch.id not in skip_ids and (include_closed or ch.category_id not in closed_ids)]
    numbers = dict(numbers) if numbers else {ch.id: n for n, ch in enumerate(ordered_channels, 1)}

    # --- 找出兩串分類 ---
    boost_prefix, boost_chain = boost_category_chain(guild)
    boost_ids = {c.id for c in boost_chain}
    coin_probe = [ch for ch in tickets_all if ch.category_id not in boost_ids]
    coin_prefix, coin_chain = ticket_category_chain(guild, coin_probe or ordered_channels, for_arrange=True)
    if not coin_chain and not boost_chain:
        res['skipped'] = True
        return res
    if coin_chain and _base_ticket_category(guild) is None:
        try:
            setv(guild.id, 'ticket_category_id', coin_chain[0].id); res['inferred'] = coin_chain[0].name
        except Exception as e:
            print(f'[ARRANGE] 自動設定開單分類失敗｜error={e!r}')

    # --- 依服務分組 ---
    by_svc = {'幣號': [], '代肝': []}
    for ch in tickets_all:
        sv = _ticket_service(ch)
        if sv is None:
            sv = '代肝' if ch.category_id in boost_ids else '幣號'
        if sv == BULLET_SERVICE: sv = '幣號'   # 子彈工單與幣號共用同一串分類
        by_svc[sv].append(ch)
    if by_svc['代肝'] and not boost_chain:
        try:
            ow = (coin_chain[0].overwrites if coin_chain else {})
            new_cat = await guild.create_category(name='代肝工單', overwrites=ow, reason='CT小舖 建立代肝工單分類')
            boost_prefix, boost_chain = '代肝工單', [new_cat]; res['created'] += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f'[ARRANGE] 建立代肝工單分類失敗｜error={e!r}'); note(e)
            by_svc['代肝'] = []   # 建不出來就不動代肝工單，絕不放進幣號分類
    if by_svc['幣號'] and not coin_chain:
        by_svc['幣號'] = []

    ticket_ids = {ch.id for ch in tickets_all}
    groups = []   # [{'svc','prefix','cats','caps','buckets','tickets'}]
    for svc, prefix, chain in (('幣號', coin_prefix, coin_chain), ('代肝', boost_prefix, boost_chain)):
        if not by_svc[svc]: continue
        cats = list(chain)
        groups.append({'svc': svc, 'prefix': prefix, 'cats': cats,
                       'caps': [max(0, L - sum(1 for x in c.channels if x.id not in ticket_ids)) for c in cats],
                       'buckets': [[] for _ in cats], 'tickets': by_svc[svc]})

    async def grow(gp):
        try:
            new_cat = await _create_next_chain_category(guild, gp['cats'], gp['prefix'])
        except Exception as e:
            print(f'[ARRANGE] 建立下一個分類失敗｜error={e!r}'); note(e); return False
        gp['cats'].append(new_cat); gp['caps'].append(L); gp['buckets'].append([]); res['created'] += 1
        await asyncio.sleep(0.5)
        return True

    for gp in groups:
        # 同一服務內，由小到大依序放：第 k 張（1 起算）放第 (k-1)//50 個分類。
        for k, ch in enumerate(gp['tickets']):
            idx = k // L
            while idx >= len(gp['cats']):
                if not await grow(gp): idx = len(gp['cats']) - 1; break
            gp['buckets'][idx].append(ch)
        # 分類裡若有非工單頻道佔位子放不下，多出來的（號碼較大的）順延到下一個分類。
        i = 0
        while i < len(gp['buckets']):
            if len(gp['buckets'][i]) > gp['caps'][i]:
                spill = gp['buckets'][i][gp['caps'][i]:]; gp['buckets'][i] = gp['buckets'][i][:gp['caps'][i]]
                if i + 1 >= len(gp['cats']) and not await grow(gp):
                    res['failed'] += len(spill); break
                gp['buckets'][i + 1] = spill + gp['buckets'][i + 1]
            i += 1

    plan = [(ch, cat) for gp in groups for cat, b in zip(gp['cats'], gp['buckets']) for ch in b]
    all_cats = [cat for gp in groups for cat in gp['cats']]
    all_buckets = [b for gp in groups for b in gp['buckets']]
    for gp in groups:
        for cat, b in zip(gp['cats'], gp['buckets']):
            if b: res['layout'].append(f'{cat.name}：{numbers[b[0].id]:04d}–{numbers[b[-1].id]:04d}（{len(b)} 張）')
    res['chain'] = [c.name for c in all_cats]

    # 方式一（優先）：一次請求就把「所在分類＋順序」全部設好。
    plan_ids = {ch.id for ch, _ in plan}
    payload = []
    for cat, b in zip(all_cats, all_buckets):
        if not b: continue
        # 分類裡的非工單頻道不放進請求（維持原位不動）；工單從它們後面接著排。
        pos = max((x.position for x in cat.text_channels if x.id not in plan_ids), default=-1) + 1
        for ch in b:
            payload.append({'id': ch.id, 'position': pos, 'parent_id': cat.id, 'lock_permissions': False}); pos += 1
    if payload:
        moved_n = sum(1 for ch, cat in plan if ch.category_id != cat.id)   # 送出請求「前」先算：之後頻道快取會被 Discord 事件更新
        try:
            await asyncio.wait_for(guild._state.http.bulk_channel_update(guild.id, payload, reason='CT小舖 自動排序工單分類'), timeout=90)
            res['moved'] = moved_n
            res['mode'] = '批次'
            await _sweep_staging(guild, plan_ids, res, note)
            return res
        except Exception as e:
            print(f'[ARRANGE] 批次更新失敗，改用逐張搬移｜error={e!r}'); note(e)
    res['mode'] = '逐張'

    # 方式二（備用）：逐張搬；目標分類暫時滿了的留到下一輪再試。
    async def _move(ch, cat, why):
        await asyncio.wait_for(ch.move(end=True, category=cat, sync_permissions=False, reason=why), timeout=90)

    pending = [(ch, cat) for ch, cat in plan if ch.category_id != cat.id]
    for _round in range(3):
        if not pending: break
        retry = []
        for ch, cat in pending:
            try:
                await _move(ch, cat, 'CT小舖 自動排序工單分類'); res['moved'] += 1
            except Exception as e:
                retry.append((ch, cat)); note(e)
            await asyncio.sleep(0.4)
        if len(retry) == len(pending): break
        pending = retry
    stuck_ids = set()
    if pending:
        stagings = []
        try:
            for n in range(0, len(pending), L):
                st = await guild.create_category(name=f'整理中暫存{n // L + 1}', overwrites=all_cats[0].overwrites,
                                                 reason='CT小舖 分類互相卡住，暫存工單用')
                held = []
                for ch, cat in pending[n:n + L]:
                    try:
                        await _move(ch, st, 'CT小舖 暫存工單'); held.append((ch, cat))
                    except Exception as e:
                        print(f'[ARRANGE] 暫存失敗｜channel={ch.id}｜error={e!r}'); res['failed'] += 1; note(e)
                    await asyncio.sleep(0.4)
                stagings.append((st, held))
        except Exception as e:
            print(f'[ARRANGE] 建立暫存分類失敗｜error={e!r}'); note(e)
            res['failed'] += len(pending) if not stagings else 0
        for _round in range(2):
            for st, held in stagings:
                for item in list(held):
                    ch, cat = item
                    try:
                        await _move(ch, cat, 'CT小舖 自動排序工單分類'); res['moved'] += 1; held.remove(item)
                    except Exception as e:
                        print(f'[ARRANGE] 搬入失敗｜channel={ch.id}｜error={e!r}'); note(e)
                    await asyncio.sleep(0.4)
        for st, held in stagings:
            if held:
                res['failed'] += len(held)
                stuck_ids.update(ch.id for ch, _ in held)
                print(f'[ARRANGE] 暫存分類 {st.name} 仍有 {len(held)} 張工單未搬出，保留該分類')
    await asyncio.sleep(1.5)
    for cat, b in zip(all_cats, all_buckets):
        if len(b) < 2: continue
        for ch in b:
            try:
                await asyncio.wait_for(ch.move(end=True, category=cat, sync_permissions=False, reason='CT小舖 自動排序工單順序'), timeout=90)
            except Exception as e:
                print(f'[ARRANGE] 排序失敗｜category={cat.id}｜channel={ch.id}｜error={e!r}'); res['failed'] += 1; note(e)
                break
            await asyncio.sleep(0.4)
    await _sweep_staging(guild, plan_ids - stuck_ids, res, note)
    return res

async def get_ticket_creation_category(guild):
    """/設定開單分類 設定的新工單分類；沒設定或已被刪除時回傳 None。"""
    return _base_ticket_category(guild)

async def ensure_service_category(guild, service):
    """取得「幣號／代肝」結單分類；尚未設定或已被刪除時回傳 None。"""
    key_ = 'service_category_boost_id' if str(service) == '代肝' else 'service_category_coin_id'
    cid = str(get(guild.id, key_, '') or '')
    if cid.isdigit():
        cat = guild.get_channel(int(cid))
        if isinstance(cat, discord.CategoryChannel):
            return cat
    return None

async def move_closed_ticket_to_service_category(ch, service_type='幣號'):
    """把結單的工單移入對應服務的結單分類。成功（或本來就在裡面）回傳 True。"""
    if not isinstance(ch, discord.TextChannel):
        return False
    service = '代肝' if str(service_type) == '代肝' else '幣號'
    cat = await ensure_service_category(ch.guild, service)
    if not isinstance(cat, discord.CategoryChannel):
        print(f'[MOVE_CLOSED] 尚未設定「{service}」結單分類｜guild={ch.guild.id}｜請用 /建立服務工單分類 或 /設定服務工單分類')
        return False
    if ch.category_id == cat.id:
        return True
    if len(cat.channels) >= 50:
        print(f'[MOVE_CLOSED] 結單分類已滿 50 個頻道｜category={cat.id}')
        return False
    return await _safe_edit_channel(ch, timeout=15, category=cat, reason='CT小舖 結單移入服務分類')

def _channel_ticket_number(ch):
    if not isinstance(ch, discord.TextChannel):
        return None
    return _resolve_ticket_number(ch)

def _auto_category_config(guild_id):
    raw = str(get(guild_id, 'auto_category_source_ids', '') or '')
    dest = str(get(guild_id, 'auto_category_destination_id', '') or '')
    mode = get(guild_id, 'auto_category_sort_mode', AUTO_CATEGORY_MODES[0])
    if mode not in AUTO_CATEGORY_MODES:
        mode = AUTO_CATEGORY_MODES[0]
    return {
        'enabled': get(guild_id, 'auto_category_enabled', '0') == '1',
        'source_ids': [int(x) for x in re.findall(r'\d+', raw)],
        'destination_id': int(dest) if dest.isdigit() else None,
        'sort_mode': mode,
    }

async def auto_categorize_channels(guild, source_ids, destination_id, sort_mode):
    """把來源分類內的文字頻道移到目標分類，並依 sort_mode 排序目標分類。回傳 (已移動, 失敗, 錯誤訊息)。"""
    dest = guild.get_channel(int(destination_id)) if destination_id else None
    if not isinstance(dest, discord.CategoryChannel):
        return 0, 0, '目標分類不存在或已被刪除，請重新使用 `/設定自動分類`。'
    sources = [guild.get_channel(int(x)) for x in (source_ids or [])]
    sources = [c for c in sources if isinstance(c, discord.CategoryChannel) and c.id != dest.id]
    to_move = [c for cat in sources for c in cat.text_channels]
    room = max(0, 50 - len(dest.channels))
    failed = max(0, len(to_move) - room)
    to_move = to_move[:room]
    pool = list(dest.text_channels) + to_move
    if not pool:
        return 0, failed, None

    def num(ch):
        n = _resolve_ticket_number(ch)
        return int(n) if n is not None else None
    mode = sort_mode if sort_mode in AUTO_CATEGORY_MODES else AUTO_CATEGORY_MODES[0]
    if mode == '工單號小-大':
        ordered = sorted(pool, key=lambda c: (num(c) is None, num(c) or 0, c.position, c.id))
    elif mode == '工單號大-小':
        ordered = sorted(pool, key=lambda c: (num(c) is None, -(num(c) or 0), c.position, c.id))
    elif mode == '隨機':
        ordered = pool[:]; random.shuffle(ordered)
    else:
        ordered = pool[:]  # 按照目前排序狀況：原有順序不動，新移入的接在後面

    moving_ids = {c.id for c in to_move}
    payload = []
    for pos, c in enumerate(ordered):
        item = {'id': c.id, 'position': pos}
        if c.id in moving_ids:
            item['parent_id'] = dest.id
            item['lock_permissions'] = False
        payload.append(item)
    try:
        await bot.http.bulk_channel_update(guild.id, payload, reason='CT小舖 自動分類')
    except (AttributeError, TypeError):
        # discord.py 版本沒有 bulk API 時，逐一處理。
        ok_count = 0
        for pos, c in enumerate(ordered):
            kw = {'position': pos}
            if c.id in moving_ids: kw['category'] = dest
            if await _safe_edit_channel(c, timeout=15, reason='CT小舖 自動分類', **kw):
                if c.id in moving_ids: ok_count += 1
            elif c.id in moving_ids:
                failed += 1
            await asyncio.sleep(0.3)
        return ok_count, failed, None
    except (discord.Forbidden, discord.HTTPException) as e:
        return 0, failed + len(to_move), f'Discord 拒絕操作：{e}'
    return len(to_move), failed, None

class AutoCategorySetupView(discord.ui.View):
    """/設定自動分類：選來源分類、目標分類、排序方式，按儲存後開啟自動分類。"""
    def __init__(self, guild, operator_id=None):
        super().__init__(timeout=600)
        self.guild = guild
        self.operator_id = operator_id
        cfg = _auto_category_config(guild.id)
        self.source_ids = list(cfg['source_ids'])
        self.destination_id = cfg['destination_id']
        self.sort_mode = cfg['sort_mode']

        self.source_select = discord.ui.ChannelSelect(placeholder='① 選擇來源分類（可複選）', channel_types=[discord.ChannelType.category], min_values=1, max_values=10, row=0)
        self.source_select.callback = self.on_source
        self.add_item(self.source_select)
        self.dest_select = discord.ui.ChannelSelect(placeholder='② 選擇目標分類', channel_types=[discord.ChannelType.category], min_values=1, max_values=1, row=1)
        self.dest_select.callback = self.on_dest
        self.add_item(self.dest_select)
        self.mode_select = discord.ui.Select(placeholder='③ 選擇排序方式', options=[discord.SelectOption(label=m, value=m, default=(m == self.sort_mode)) for m in AUTO_CATEGORY_MODES], min_values=1, max_values=1, row=2)
        self.mode_select.callback = self.on_mode
        self.add_item(self.mode_select)
        self.save_button = discord.ui.Button(label='💾 儲存並開啟自動分類', style=discord.ButtonStyle.success, row=3)
        self.save_button.callback = self.on_save
        self.add_item(self.save_button)

    def _name(self, guild, cid):
        ch = guild.get_channel(int(cid)) if cid else None
        return ch.name if ch else f'（找不到 {cid}）'

    def _render(self, guild=None):
        guild = guild or self.guild
        src = '、'.join(f'**{self._name(guild, x)}**' for x in self.source_ids) or '尚未選擇'
        dst = f'**{self._name(guild, self.destination_id)}**' if self.destination_id else '尚未選擇'
        return ('🗂️ **自動分類設定**\n'
                f'① 來源分類：{src}\n② 目標分類：{dst}\n③ 排序方式：**{self.sort_mode}**\n\n'
                '選好後按「💾 儲存並開啟自動分類」。已存在的頻道可用 `/立即整理分類` 手動整理。')

    def _status(self):
        cfg = _auto_category_config(self.guild.id)
        return f'\n\n目前總開關：{"🟢 開啟" if cfg["enabled"] else "🔴 關閉"}'

    async def interaction_check(self, i):
        if not admin(i.user) or (self.operator_id and i.user.id != self.operator_id):
            await i.response.send_message('❌ 只有開啟這個面板的管理員可以操作。', ephemeral=True)
            return False
        return True

    async def _refresh(self, i):
        await i.response.edit_message(content=self._render(i.guild) + self._status(), view=self)

    async def on_source(self, i):
        self.source_ids = [c.id for c in self.source_select.values]
        await self._refresh(i)

    async def on_dest(self, i):
        vals = self.dest_select.values
        self.destination_id = vals[0].id if vals else None
        await self._refresh(i)

    async def on_mode(self, i):
        self.sort_mode = self.mode_select.values[0]
        for o in self.mode_select.options:
            o.default = (o.value == self.sort_mode)
        await self._refresh(i)

    async def on_save(self, i):
        if not self.source_ids or not self.destination_id:
            return await i.response.send_message('❌ 請先選擇來源分類與目標分類。', ephemeral=True)
        if self.destination_id in self.source_ids:
            return await i.response.send_message('❌ 目標分類不能同時是來源分類。', ephemeral=True)
        gid = i.guild.id
        setv(gid, 'auto_category_source_ids', ','.join(str(x) for x in self.source_ids))
        setv(gid, 'auto_category_destination_id', self.destination_id)
        setv(gid, 'auto_category_sort_mode', self.sort_mode)
        setv(gid, 'auto_category_enabled', '1')
        await i.response.edit_message(content='✅ **自動分類已儲存並開啟**\n' + self._render(i.guild).split('\n\n')[0].split('\n', 1)[1] + '\n\n新建立在來源分類的頻道會自動移到目標分類；現有頻道請用 `/立即整理分類`。', view=None)

async def _auto_category_on_create(ch):
    """新頻道建立在來源分類時，自動依設定整理（只在自動分類開啟時）。"""
    try:
        cfg = _auto_category_config(ch.guild.id)
        if not cfg['enabled'] or not cfg['destination_id'] or ch.category_id not in cfg['source_ids']:
            return
        lk = AUTO_CATEGORY_LOCKS.setdefault(ch.guild.id, asyncio.Lock())
        async with lk:
            await asyncio.sleep(3)
            _moved, _failed, err = await auto_categorize_channels(ch.guild, cfg['source_ids'], cfg['destination_id'], cfg['sort_mode'])
            if err: print(f'[AUTO_CATEGORY] 自動整理失敗｜guild={ch.guild.id}｜{err}')
    except Exception as e:
        print(f'[AUTO_CATEGORY] 例外｜error={e!r}'); traceback.print_exc()

def _replace_ticket_number_in_name(name, new_no):
    """只替換名稱中的工單號那一段，保留其他格式。找不到回傳 None。"""
    n = name or ''
    m = re.search(r'(?<=[-_ #])(\d{1,8})(?![\dmMｍ]|\s*[隻發])', n)
    if not m:
        return None
    return n[:m.start(1)] + new_no + n[m.end(1):]

async def renumber_all_tickets(guild, include_closed=False):
    """排序期間暫停開單（共用開單鎖），避免新單撞號或被排進錯的分類。"""
    async with ticket_open_lock:
        return await _renumber_all_tickets_impl(guild, include_closed)

def _ticket_in_service(ch):
    """工單是否放在「服務中」分類。排序時這類工單不改名、不換號、不搬分類。"""
    cat = getattr(ch, 'category', None)
    return cat is not None and IN_SERVICE_CATEGORY_KEYWORD in (cat.name or '')

async def _sort_closed_categories(guild, numbers):
    """結單分類（幣號結單／代肝結單）不會被搬動，但裡面的工單要依工單號由小到大排好。
    沒有工單號的頻道完全不動：工單只在「工單原本佔的位子」之間重新排列。回傳排序了幾張工單。"""
    cats = [guild.get_channel(i) for i in _closed_category_ids(guild)]
    cats = [c for c in cats if isinstance(c, discord.CategoryChannel)]
    payload = []; total = 0
    for cat in cats:
        chs = list(cat.text_channels)
        slots = [c for c in chs if c.id in numbers]
        if len(slots) < 2: continue
        slot_positions = [c.position for c in slots]
        ordered = sorted(slots, key=lambda c: (numbers[c.id], c.id))
        for pos, ch in zip(slot_positions, ordered):
            payload.append({'id': ch.id, 'position': pos})
        total += len(slots)
    if payload:
        await asyncio.wait_for(guild._state.http.bulk_channel_update(guild.id, payload, reason='CT小舖 結單分類依工單號排序'), timeout=90)
    return total

async def _renumber_all_tickets_impl(guild, include_closed=False):
    """依工單開啟時間（越早越小），把所有工單重新連號為 0001、0002…；同步改頻道名稱與資料庫。"""
    async with RENUMBER_LOCK:
        items = []
        for ch in guild.text_channels:
            try:
                if not is_ticket_candidate(ch): continue
                no = _resolve_ticket_number(ch)
            except Exception as e:
                print(f'[RENUMBER] 辨識失敗｜channel={ch.id}｜error={e!r}'); continue
            if no is None: continue
            # 名稱裡沒有工單號的頻道（例如手動命名的「退費的雞掰人」）：完全不動——不改名、不編號、不搬分類。
            if ticket_no(ch.name) is None and _loose_ticket_no(ch) is None: continue
            if _replace_ticket_number_in_name(ch.name, '0000') is None: continue
            # 依「工單開啟時間」排序（頻道建立時間，越早越小）；時間相同才比頻道 ID。
            items.append((ch.created_at, ch.id, no, ch))
        items.sort(key=lambda x: (x[0], x[1]))
        mapping = []; changed = 0; failed = 0; refresh_channels = []; failed_ids = set()
        # 「服務中」分類裡的工單：維持原號碼、原分類，完全不動；其他工單依建立時間編號，並跳過被它們佔用的號碼。
        locked = {ch.id for _, _, _, ch in items if _ticket_in_service(ch)}
        reserved = {int(old_no) for _, _, old_no, ch in items if ch.id in locked}
        new_numbers = {}; n_ = 0
        for _, _, old_no, ch in items:
            if ch.id in locked:
                new_numbers[ch.id] = int(old_no); continue
            n_ += 1
            while n_ in reserved: n_ += 1
            new_numbers[ch.id] = n_
        # 改名是最慢的一步（每個頻道一次 Discord 請求）。名稱沒變的直接略過；要改的用少量並行同時送出，
        # 並行數刻意壓低（Discord 對頻道改名有限速，discord.py 會自行排隊），整體比逐張等待快數倍。
        sem = asyncio.Semaphore(RENAME_CONCURRENCY)
        async def _apply(old_no, ch):
            new_no = f'{new_numbers[ch.id]:04d}'
            old_fmt = f'{int(old_no):04d}'
            new_name = _replace_ticket_number_in_name(ch.name, new_no)
            if new_name is None:
                return None   # 保險：名稱找不到工單號就不動（正常上面已過濾）
            if new_name != ch.name:
                async with sem:
                    ok = await _safe_edit_channel(ch, timeout=20, name=new_name, reason='CT小舖 自動排序工單號')
                    if ok: await asyncio.sleep(0.05)
                if not ok:
                    # 改名失敗（常見：被 Discord 限速）→ 這張維持舊號、留在原分類，
                    # 不能再照「新號碼」去排分類，否則會出現重號、順序錯亂。
                    return ('fail', ch, old_no)
            try:
                q('UPDATE orders SET ticket_no=? WHERE channel_id=?', (new_no, str(ch.id)))
                remember(ch, new_no)
            except sqlite3.Error as e:
                print(f'[RENUMBER] DB 更新失敗｜channel={ch.id}｜error={e!r}')
                return ('dberr', ch, old_no)
            return ('ok', ch, old_fmt, new_no)
        results = await asyncio.gather(*[_apply(old_no, ch) for _, _, old_no, ch in items if ch.id not in locked])
        for r_ in results:   # gather 依原順序回傳，結果對照表維持由小到大
            if r_ is None: continue
            if r_[0] == 'fail':
                failed += 1; failed_ids.add(r_[1].id); new_numbers[r_[1].id] = int(r_[2]); continue
            if r_[0] == 'dberr':
                failed += 1; failed_ids.add(r_[1].id); continue
            _, ch, old_fmt, new_no = r_
            mapping.append((old_fmt, new_no, ch))
            if old_fmt != new_no:
                changed += 1
                refresh_channels.append(ch)
        # 依新工單號把工單排進整串分類（幣號工單、幣號工單1、2…），滿 50 自動往下開新分類。
        arrange = {'moved': 0, 'created': 0, 'failed': 0, 'skipped': True}
        try:
            ordered_ = sorted((ch for _, _, _, ch in items), key=lambda c: (new_numbers[c.id], c.id))
            arrange = await arrange_ticket_chain(guild, ordered_, include_closed, numbers=new_numbers, skip_ids=(locked | failed_ids))
        except Exception as e:
            print(f'[RENUMBER] 分類排列失敗｜error={e!r}')
            try:
                _r = {}; await _sweep_staging(guild, set(), _r, lambda _e: None)
                arrange = dict(arrange, staging_deleted=_r.get('staging_deleted', 0), staging_kept=_r.get('staging_kept', 0))
            except Exception as e2:
                print(f'[RENUMBER] 清理暫存分類失敗｜error={e2!r}')
        closed_sorted = 0
        try:
            closed_sorted = await _sort_closed_categories(guild, new_numbers)
        except Exception as e:
            print(f'[RENUMBER] 結單分類排序失敗｜error={e!r}')
        # 計數器重設為「現存工單數 + 1」；清掉已刪除頻道的舊紀錄，避免之後發號跳號。
        try:
            gid = str(guild.id)
            live_ids = {str(ch.id) for _, _, _, ch in items}
            for r in q('SELECT channel_id FROM ticket_channels WHERE guild_id=?', (gid,), True):
                if r['channel_id'] not in live_ids:
                    q('DELETE FROM ticket_channels WHERE channel_id=?', (r['channel_id'],))
            q('INSERT INTO ticket_counters(guild_id,next_no) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET next_no=excluded.next_no', (gid, (max(new_numbers.values()) if new_numbers else 0) + 1))
        except sqlite3.Error as e:
            print(f'[RENUMBER] 計數器更新失敗｜error={e!r}')
        # 狀態頻道裡「訂單 #xxxx」的卡片也要跟著換成新工單號（只更新已存在的卡，不補發舊單）。
        refreshed = 0
        for ch in refresh_channels:
            try:
                rows = q('SELECT o.* FROM orders o JOIN status_posts sp ON sp.order_id=o.id WHERE o.channel_id=?', (str(ch.id),), True)
                for o in rows:
                    await status_announce(o, guild); refreshed += 1
                    await asyncio.sleep(0.3)
            except Exception as e:
                print(f'[RENUMBER] 更新狀態卡失敗｜channel={ch.id}｜error={e!r}')
        from collections import Counter
        collisions = sorted(n for n, c in Counter(new_numbers.values()).items() if c > 1)
        return {'found': len(items), 'changed': changed, 'failed': failed, 'mapping': mapping, 'refreshed': refreshed, 'arrange': arrange, 'in_service': len(locked), 'collisions': collisions, 'closed_sorted': closed_sorted}

# === /改名工單：管理員快速改名；幣號數量使用與客人購買相同的原生 Modal ===
# 幣號／代肝各自固定的狀態與額度（使用固定 choices，Discord 端立即顯示，不需等機器人回應）
RENAME_COIN_STATUSES = ('待付款', '待交貨', '待處理', '結單')
RENAME_BOOST_STATUSES = ('待付款', '待倒', '待處理', '結單')
RENAME_COIN_AMOUNTS = ('50M', '100M', '200M')
RENAME_BOOST_AMOUNTS = tuple(BOOST_TIERS)   # 50M 100M 150M 200M 300M 400M 500M … 1000M

def _loose_ticket_no(ch):
    """放在工單分類（開單分類串／結單分類）裡、名稱帶有「0xxx」工單號，但名稱不符合標準格式的頻道，
    例如「退款成餘額-0289」「已退款-0299-80」。這類手動改名的工單過去不會被辨識，
    所以排序時被略過，留著舊號、佔著位子，看起來就亂掉。"""
    if not isinstance(ch, discord.TextChannel): return None
    cat = getattr(ch, 'category', None)
    if cat is None: return None
    try:
        ids = set(_closed_category_ids(ch.guild))
        _, chain = ticket_category_chain(ch.guild)
        ids |= {c.id for c in chain}
    except Exception:
        return None
    if cat.id not in ids: return None
    m_ = re.search(r'(?<![\dA-Za-z])(0\d{3})(?![\dmMｍ])', ch.name or '')
    return m_.group(1) if m_ else None

def _resolve_ticket_number(ch):
    no=ticket_no(ch.name) if isinstance(ch,discord.TextChannel) else None
    if no is None and isinstance(ch,discord.TextChannel):
        rows=q('SELECT ticket_no FROM ticket_channels WHERE channel_id=? LIMIT 1',(str(ch.id),),True)
        if rows: no=str(rows[0]['ticket_no'])
    if no is None and isinstance(ch,discord.TextChannel):
        rows=q('SELECT ticket_no FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(ch.id),),True)
        if rows: no=str(rows[0]['ticket_no'])
    if no is None and isinstance(ch,discord.TextChannel):
        no=_loose_ticket_no(ch)
    return f'{int(no):04d}' if no is not None and str(no).isdigit() else None

async def _complete_manual_rename(i, service, status, amount, qty):
    # 呼叫前必須已 defer；這裡一律用 followup 回覆，避免互動逾時（Discord 3 秒限制）。
    async def reply(msg):
        return await i.followup.send(msg,ephemeral=True)
    ch=i.channel
    if not isinstance(ch,discord.TextChannel):
        return await reply('❌ 請在工單頻道內使用。')
    no=_resolve_ticket_number(ch)
    if not no:
        return await reply('❌ 無法自動辨識這張工單的工單號，未執行改名。')
    rows=q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(ch.id),),True)
    order=rows[0] if rows else None
    old_status=str(order['status']) if order else (_status_from_name(ch.name) or '')
    old_service=str(order['service_type']) if order and order['service_type'] else (_service_from_name(ch.name) or service)
    new_name=rename_name(status,no,amount,qty,service_type=service)

    old_values=None
    if order:
        old_values=(order['ticket_no'],order['service_type'],order['status'],order['product'],order['quantity'],order['completed_at'])
        try:
            q('UPDATE orders SET ticket_no=?,service_type=?,status=?,product=?,quantity=?,completed_at=? WHERE id=?',
              (no,service,status,amount,qty,now() if status=='結單' else None,order['id']))
        except sqlite3.Error as e:
            return await reply(f'❌ 工單資料更新失敗，未改名：`{str(e)[:180]}`')

    def rollback():
        if order and old_values is not None:
            try:
                q('UPDATE orders SET ticket_no=?,service_type=?,status=?,product=?,quantity=?,completed_at=? WHERE id=?',
                  (*old_values,order['id']))
            except sqlite3.Error as rb:
                print(f'[RENAME_TICKET] DB rollback failed｜channel={ch.id}｜error={rb!r}')

    # Discord 對「頻道改名」有頻率限制（約 10 分鐘 2 次），超過時 discord.py 會一直等待，
    # 因此加上逾時，避免指令卡死。
    try:
        if ch.name!=new_name:
            await asyncio.wait_for(ch.edit(name=new_name,reason=f'CT小舖 /改名工單｜操作者 {i.user.id}'),timeout=20)
    except asyncio.TimeoutError:
        rollback()
        return await reply('⏳ Discord 頻道改名被限速（同一頻道約 10 分鐘內只能改名 2 次），未改名、資料已回復。請稍後再試。')
    except (discord.Forbidden,discord.HTTPException) as e:
        rollback()
        return await reply(f'❌ 工單改名失敗，資料已回復：`{str(e)[:180]}`')
    except Exception as e:
        traceback.print_exc()
        rollback()
        return await reply(f'❌ 改名發生未預期錯誤，資料已回復：`{str(e)[:180]}`')

    warn=''
    try:
        remember(ch,no,order['buyer_id'] if order else None)
        buyer_id=str(order['buyer_id']) if order and order['buyer_id'] else ''
        extra=await sync_ticket_state_after_rename(ch,old_status,status,buyer_id,service)
        if extra: warn+=extra
        if order:
            release_reserved_stock(order['id'],old_status,status)
            refunded=refund_balance_on_cancel(order['id'],old_status,status,i.user.id)
            if refunded: warn+=f'\n💰 已把餘額付款 NT${refunded:,} 退回客人的餘額。'
            rr=q('SELECT * FROM orders WHERE id=?',(order['id'],),True)[0]
            log_order(order['id'],i.guild.id,i.user.id,'改名工單',f'{old_service}/{old_status} → {service}/{status}｜{new_name}'+(f'｜已退回餘額 NT${refunded:,}' if refunded else ''))
            await status_announce(rr,i.guild)
    except Exception as e:
        traceback.print_exc()
        warn=f'\n⚠️ 名稱已更新，但後續同步（分類／權限／狀態公告）發生錯誤：`{str(e)[:150]}`'
    await reply(f'✅ 已直接改名為：`{new_name}`{warn}')


rename_group=app_commands.Group(name='改名工單',description='直接依選擇的狀態與額度改名目前工單')

async def _rename_entry(i: discord.Interaction,service:str,狀態:str,額度:str,數量:int=1):
    if not admin(i.user):
        return await i.response.send_message('❌ 你沒有權限使用這個指令。',ephemeral=True)
    # 先 defer，後面的改名／同步可能超過 3 秒。
    await i.response.defer(ephemeral=True)
    try:
        ch=i.channel
        if not isinstance(ch,discord.TextChannel):
            return await i.followup.send('❌ 請在工單頻道內使用 `/改名工單`。',ephemeral=True)
        status=str(狀態).strip(); amount=str(額度).strip().upper().replace('Ｍ','M')
        allowed_statuses=RENAME_BOOST_STATUSES if service=='代肝' else RENAME_COIN_STATUSES
        allowed_amounts=RENAME_BOOST_AMOUNTS if service=='代肝' else ((BULLET_PRODUCT,) if service==BULLET_SERVICE else RENAME_COIN_AMOUNTS)
        if status not in allowed_statuses:
            return await i.followup.send('❌ 狀態與服務不符合，請重新選擇。',ephemeral=True)
        if amount not in allowed_amounts:
            return await i.followup.send('❌ 額度與服務不符合，請重新選擇。',ephemeral=True)
        if not _resolve_ticket_number(ch):
            return await i.followup.send('❌ 無法自動辨識這張工單的工單號，未執行改名。',ephemeral=True)
        if service=='幣號' and not 1<=int(數量)<=1000:
            return await i.followup.send('❌ 幣號數量請輸入 1～1000。',ephemeral=True)
        if service==BULLET_SERVICE and not 1<=int(數量)<=100000:
            return await i.followup.send('❌ 子彈數量請輸入 1～100000。',ephemeral=True)
        qty=1 if service=='代肝' else int(數量)
        return await _complete_manual_rename(i,service,status,amount,qty)
    except Exception as e:
        traceback.print_exc()
        try: await i.followup.send(f'❌ /改名工單 發生錯誤：`{str(e)[:180]}`',ephemeral=True)
        except Exception: pass

@rename_group.command(name='幣號',description='幣號工單改名')
@app_commands.describe(狀態='幣號狀態',額度='幣號額度',數量='幣號數量（1～1000）')
@app_commands.choices(狀態=choices(RENAME_COIN_STATUSES),額度=choices(RENAME_COIN_AMOUNTS))
async def rename_coin_command(i: discord.Interaction,狀態:str,額度:str,數量:app_commands.Range[int,1,1000]=1):
    await _rename_entry(i,'幣號',狀態,額度,數量)

@rename_group.command(name='代肝',description='代肝工單改名')
@app_commands.describe(狀態='代肝狀態',額度='代肝額度')
@app_commands.choices(狀態=choices(RENAME_BOOST_STATUSES),額度=choices(RENAME_BOOST_AMOUNTS))
async def rename_boost_command(i: discord.Interaction,狀態:str,額度:str):
    await _rename_entry(i,'代肝',狀態,額度,1)

@rename_group.command(name='子彈',description='A大子彈工單改名')
@app_commands.describe(狀態='子彈狀態',數量='子彈數量（發）')
@app_commands.choices(狀態=choices(RENAME_COIN_STATUSES))
async def rename_bullet_command(i: discord.Interaction,狀態:str,數量:app_commands.Range[int,1,100000]=1):
    await _rename_entry(i,BULLET_SERVICE,狀態,BULLET_PRODUCT,數量)

bot.tree.add_command(rename_group)

@bot.tree.command(name='修改狀態',description='修改目前工單狀態')
@app_commands.describe(工單='選擇工單頻道',狀態='新狀態')
@app_commands.choices(狀態=choices(STATUSES))
async def change_status(i,工單:discord.TextChannel,狀態:str):
    if not admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    try:
        r=q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(工單.id),),True)
        if not r: return await i.followup.send('❌ 找不到這張工單的訂單紀錄。',ephemeral=True)
        o=r[0]; old_status=str(o['status'])
        if 狀態=='已取消' and balance_refund_pending(o,old_status) and not strict_admin(i.user):
            return await i.followup.send('❌ 這張訂單是用餘額付款的，改成「已取消」會自動把錢退回客人餘額，只有**店長等級**可以操作。',ephemeral=True)
        q('UPDATE orders SET status=?,completed_at=? WHERE id=?',(狀態,now() if 狀態=='結單' else None,o['id']))
        released=release_reserved_stock(o['id'],old_status,狀態)
        refunded=refund_balance_on_cancel(o['id'],old_status,狀態,i.user.id)
        name=rename_name(狀態,o['ticket_no'],o['product'],o['quantity'],service_type=o['service_type'])
        rename_ok=True
        if 工單.name!=name:
            rename_ok=await _safe_edit_channel(工單,timeout=15,name=name,reason=f'CT小舖 /修改狀態｜操作者 {i.user.id}')
        warn=''
        try:
            extra=await sync_ticket_state_after_rename(工單,old_status,狀態,o['buyer_id'],str(o['service_type'] or '幣號'))
            if extra: warn+=extra
            log_order(o['id'],i.guild.id,i.user.id,'修改狀態',f'{old_status} -> {狀態}'+('｜已退回預扣庫存' if released else '')+(f'｜已退回餘額 NT${refunded:,}' if refunded else '')); await post_channel_log(i.guild,'action_log_channel_id',f'👑 **修改狀態**｜#{o["ticket_no"]}｜{old_status} → {狀態}｜{i.user.mention}')
            rr=q('SELECT * FROM orders WHERE id=?',(o['id'],),True)[0]; await status_announce(rr,i.guild)
        except Exception as e:
            traceback.print_exc(); warn=f'\n⚠️ 後續同步（分類／權限／公告）發生錯誤：`{str(e)[:150]}`'
        if not rename_ok: warn+='\n⚠️ 頻道改名失敗或被 Discord 限速（同頻道 10 分鐘內只能改名 2 次），名稱未更新。'
        if refunded: warn+=f'\n💰 已把餘額付款 NT${refunded:,} 退回客人的餘額。'
        await i.followup.send(f'✅ {工單.mention} 已更新為 **{狀態}**。{warn}',ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        try: await i.followup.send(f'❌ /修改狀態 發生錯誤：`{str(e)[:180]}`',ephemeral=True)
        except Exception: pass

PROTECTED_CHANNEL_SETTINGS=('status_channel_id','action_log_channel_id','order_log_channel_id','coin_price_channel_id','boost_price_channel_id')

def is_protected_channel(guild, ch):
    """機器人自己用的頻道（狀態公告、操作／訂單紀錄、價目表）：絕對不能被當成工單關閉、改名或搬走。"""
    try: gid=guild.id
    except AttributeError: return False
    for key in PROTECTED_CHANNEL_SETTINGS:
        if str(get(gid,key,'') or '').strip()==str(ch.id): return True
    return False

def closable_ticket_problem(guild, ch):
    """這個頻道能不能被關閉？不能就回傳原因文字，可以就回傳 None。"""
    if not isinstance(ch,discord.TextChannel): return '請在工單頻道使用。'
    if is_protected_channel(guild,ch): return '這是機器人使用中的頻道（狀態公告／紀錄／價目表），不是工單，不能關閉。'
    if not is_ticket_candidate(ch): return '這個頻道不是工單（沒有訂單、也沒有工單號），為了避免誤改一般頻道，已拒絕。'
    return None

async def close_ticket_channel(guild, ch, operator, command_label='關閉工單', revoke=True, buyer_hint=None, keep_order=False):
    """關閉一張工單（/關閉工單、/關所有工單、/關閉結單工單、客人按關閉都共用）：移除客人權限 → 結單改名 → 移入結單分類 → 紀錄與公告。
    每一步獨立保護，單一步驟失敗不會中斷。回傳 dict：ticket_number / old_status / perm_msg / moved / rename_ok。
    不是工單的頻道（一般頻道、機器人用的頻道）會丟 ValueError，不會被改名或搬走。"""
    problem=closable_ticket_problem(guild,ch)
    if problem: raise ValueError(problem)
    r=q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(ch.id),),True)
    if r:
        o=r[0]; old_status=str(o['status'] or '')
        if keep_order:
            pass     # /關閉結單工單：只看頻道名稱，不動訂單狀態、結單時間與庫存
        elif old_status=='結單':
            # 已經結單：保留原本的結單時間，只補上沒有的
            if not o['completed_at']: q('UPDATE orders SET completed_at=? WHERE id=?',(now(),o['id']))
        else:
            q('UPDATE orders SET status=?,completed_at=? WHERE id=?',('結單',now(),o['id']))
        if not keep_order: release_reserved_stock(o['id'],old_status,'結單')
        service_type=str(o['service_type'] or '幣號') if 'service_type' in o.keys() else '幣號'
        ticket_number=str(o['ticket_no']); buyer_id=o['buyer_id']
        product=str(o['product'] or '50M')
        try: qty=max(1,int(o['quantity'] or 1))
        except (TypeError,ValueError): qty=1
    else:
        ticket_number=_resolve_ticket_number(ch) or '0000'
        service_type=_service_from_name(ch.name) or '幣號'
        old_status=_status_from_name(ch.name) or '待處理'
        product,qty=_amount_qty_from_name(ch.name,service_type)
        product=product or '50M'; qty=max(1,int(qty or 1))
        buyer_id=buyer_hint or None

    # 1) 先移除客人權限（只有關閉工單才做），不要被後面的改名限速拖住
    if revoke:
        try: _ok,perm_msg=await revoke_buyer_access(ch,buyer_id)
        except Exception as e:
            traceback.print_exc(); perm_msg=f'程式錯誤：{e}'
    else:
        perm_msg='客人權限保留（未移除）'

    # 2) 改名
    final_name=rename_name('結單',f'{int(ticket_number):04d}' if str(ticket_number).isdigit() else ticket_number,product,qty,service_type=service_type)
    rename_ok=True
    if ch.name!=final_name:
        rename_ok=await _safe_edit_channel(ch,timeout=10,name=final_name,reason=f'{command_label}自動改名')

    # 3) 移分類
    moved=False
    for _ in range(2):
        moved=await move_closed_ticket_to_service_category(ch,service_type)
        if moved: break
        await asyncio.sleep(0.25)

    # 4) 紀錄／公告：失敗只印 log，不影響結果
    try:
        if r and not keep_order: log_order(r[0]['id'],guild.id,operator.id,command_label,f'{old_status} → 結單｜{perm_msg}')
        await post_channel_log(guild,'action_log_channel_id',f'👑 **{command_label}**｜#{ticket_number}｜{old_status} → 結單｜{operator.mention}｜{perm_msg}')
    except Exception as e: print(f'[CLOSE] log failed｜{e!r}')
    try:
        await ch.send(f'🔒 **工單已{"關閉" if revoke else "結單"}**\n狀態：結單\n{"🔒 "+perm_msg if revoke else ""}\n📁 結單分類：{"已移入" if moved else "移動失敗，請確認已設定結單分類且機器人有管理頻道權限"}',view=ClosedTicketView())
    except discord.HTTPException as e:
        print(f'[CLOSE] send close message failed｜channel={ch.id}｜error={e!r}')
    try:
        if r and not keep_order:
            rr=q('SELECT * FROM orders WHERE id=?',(r[0]['id'],),True)[0]
            await status_announce(rr,guild)
    except Exception as e: print(f'[CLOSE] status_announce failed｜{e!r}')
    return {'ticket_number':ticket_number,'old_status':old_status,'perm_msg':perm_msg,'moved':moved,'rename_ok':rename_ok}

async def _close_ticket_core(i, command_label='關閉工單', revoke=True):
    """/關閉工單：移除客人權限＋結單＋移入結單分類。（/結單 指令已移除；revoke 參數保留，目前一律為 True。）"""
    if not admin(i.user): return await deny(i)
    if not i.response.is_done():
        await i.response.defer(ephemeral=True)
    try:
        ch=i.channel
        problem=closable_ticket_problem(i.guild,ch)
        if problem: return await i.followup.send(f'❌ {problem}',ephemeral=True)
        res=await close_ticket_channel(i.guild,ch,i.user,command_label,revoke)
        ticket_number,perm_msg,moved,rename_ok=res['ticket_number'],res['perm_msg'],res['moved'],res['rename_ok']
        await i.followup.send(f'✅ 工單 #{ticket_number} 已{"關閉" if revoke else "結單"}。\n{"🔒 "+perm_msg if revoke else "客人權限保留，未移除。"}\n📁 {"已移入對應結單分類" if moved else "結單分類移動失敗：請先用 /建立服務工單分類 或 /設定服務工單分類 設定，並確認機器人有管理頻道權限"}'+('' if rename_ok else '\n⚠️ 頻道改名失敗或被 Discord 限速（同頻道 10 分鐘內只能改名 2 次），名稱未更新。'),ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        try: await i.followup.send(f'❌ /{command_label} 發生錯誤：`{type(e).__name__}: {str(e)[:180]}`',ephemeral=True)
        except Exception: pass

# ===== /關所有工單：一次關閉很多張（可選狀態）=====
CLOSE_ALL_ALL = '全部（不含結單）'
CLOSE_ALL_NO_ORDER = '無訂單'
CLOSE_ALL_MAX_PER_RUN = 80          # 每次最多處理幾張（關一張要改權限、改名、移分類，約 2～4 秒），超過的再執行一次
_CLOSE_ALL_RUNNING = set()
# 試過但還是關不好的結單工單（例如結單分類滿 50 個頻道、機器人沒權限）。
# 不記起來的話，它們永遠排在最前面，每次都佔掉前 80 個名額，後面的就永遠輪不到。
_CLOSE_STUCK = {}                   # {channel_id(str): (時間戳, 原因)}
CLOSE_STUCK_TTL = 6 * 3600          # 6 小時內不再重試（重啟機器人也會清空）

def _stuck_active(cid):
    v = _CLOSE_STUCK.get(str(cid))
    if not v: return False
    if datetime.now().timestamp() - v[0] > CLOSE_STUCK_TTL:
        _CLOSE_STUCK.pop(str(cid), None); return False
    return True

def ticket_current_status(ch):
    """工單目前狀態：以訂單資料為準，沒有訂單就看頻道名稱；兩者都沒有就是「無訂單」。"""
    r = q('SELECT status FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1', (str(ch.id),), True)
    if r and r[0]['status']: return str(r[0]['status'])
    return _status_from_name(ch.name) or CLOSE_ALL_NO_ORDER

def find_tickets_to_close(guild, status):
    """找出要關的工單 [(頻道, 狀態)]。已經是結單（或名稱看起來已關閉）的永遠不算。"""
    out = []
    for ch in guild.text_channels:
        try:
            if not is_ticket_candidate(ch) or is_protected_channel(guild, ch): continue
            st = ticket_current_status(ch)
        except Exception as e:
            print(f'[CLOSE_ALL] 辨識失敗｜channel={getattr(ch, "id", "?")}｜error={e!r}'); continue
        if st == '結單' or looks_closed(ch.name): continue
        if status != CLOSE_ALL_ALL and st != status: continue
        out.append((ch, st))
    out.sort(key=lambda x: (x[0].created_at, x[0].id))
    return out

# ===== /關閉結單工單：已經是「結單」，但客人還看得到、或還沒進結單分類的工單 =====
CLOSE_DONE_STATUS = '結單'

def closed_category_id(guild_id, service):
    """設定好的結單分類 ID（幣號／代肝）；沒設定回傳 None。"""
    key = 'service_category_boost_id' if str(service) == '代肝' else 'service_category_coin_id'
    cid = str(get(guild_id, key, '') or '')
    return int(cid) if cid.isdigit() else None

def ticket_buyer_id_quick(ch):
    """不用呼叫 Discord，只查資料庫就拿到工單客人 ID（沒有就回傳空字串）。"""
    r = q('SELECT buyer_id FROM orders WHERE channel_id=? AND buyer_id IS NOT NULL ORDER BY id DESC LIMIT 1', (str(ch.id),), True)
    if r and r[0]['buyer_id']: return str(r[0]['buyer_id'])
    rec = ticket_record(ch)
    return str(rec['buyer_id']) if rec and rec['buyer_id'] else ''

_VISIBLE_MEMBER_CACHE = {}   # {(guild_id, user_id): 是不是「還看得到頻道的客人／闆闆」}，每次批量開始前清空

async def _member_is_customer_like(ch, target):
    """跟 revoke_buyer_access 同一套判斷：非機器人、非店長／管理員（闆闆身分組除外）的個人覆寫，視為客人。"""
    key = (ch.guild.id, target.id)
    if key in _VISIBLE_MEMBER_CACHE: return _VISIBLE_MEMBER_CACHE[key]
    me = ch.guild.me
    m = target if isinstance(target, discord.Member) else ch.guild.get_member(target.id)
    if m is None:
        try: m = await ch.guild.fetch_member(target.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            _VISIBLE_MEMBER_CACHE[key] = False; return False     # 已離開伺服器：本來就看不到
    role = _find_boss_role(ch.guild)
    is_boss = bool(role and role in m.roles)
    res = not (m.bot or (me and m.id == me.id) or (_is_staff_member(m) and not is_boss))
    _VISIBLE_MEMBER_CACHE[key] = res
    return res

async def ticket_not_really_closed(guild, ch):
    """結單的工單還差什麼才算真正關閉？回傳原因清單（空＝已經關好了）。
    ① 頻道上仍有「看得見」的客人個人權限覆寫（不只看資料庫記的買家，Ticket Tool 開的舊單也算）；
    ② 設定了結單分類，但工單不在裡面。"""
    reasons = []
    if boss_role_can_see(ch): reasons.append('闆闆身分組仍看得到')
    try:
        for target, ow in list(getattr(ch, 'overwrites', {}).items()):
            if isinstance(target, discord.Role): continue
            if getattr(ow, 'view_channel', None) is False: continue
            if await _member_is_customer_like(ch, target): reasons.append('客人仍看得到'); break
    except Exception as e:
        print(f'[CLOSE_DONE] 檢查客人權限失敗｜channel={ch.id}｜error={e!r}')
    r = q('SELECT service_type FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1', (str(ch.id),), True)
    service = (str(r[0]['service_type']) if r and r[0]['service_type'] else None) or _service_from_name(ch.name) or '幣號'
    cat_id = closed_category_id(guild.id, service)
    if cat_id and getattr(ch, 'category_id', None) != cat_id: reasons.append('不在結單分類')
    return reasons

def name_is_done(ch):
    """/關閉結單工單只看頻道名稱：名稱裡有「結單」就算，完全不管訂單狀態。"""
    return CLOSE_DONE_STATUS in (getattr(ch, 'name', '') or '')

async def find_done_tickets_to_close(guild):
    """找出「狀態已是結單、但還沒真正關閉」的工單 [(頻道, 原因文字)]，由舊到新。"""
    _VISIBLE_MEMBER_CACHE.clear()
    out = []
    for ch in guild.text_channels:
        try:
            if not is_ticket_candidate(ch) or is_protected_channel(guild, ch): continue
            if _stuck_active(ch.id): continue
            if not name_is_done(ch): continue
            reasons = await ticket_not_really_closed(guild, ch)
        except Exception as e:
            print(f'[CLOSE_DONE] 辨識失敗｜channel={getattr(ch, "id", "?")}｜error={e!r}'); continue
        if reasons: out.append((ch, '、'.join(reasons)))
    out.sort(key=lambda x: (x[0].created_at, x[0].id))
    return out

async def run_close_all(i, status, ids, mode='normal'):
    """依預覽時的清單逐張關閉；關之前再確認一次狀態（預覽之後狀態變了的不動），一張失敗不影響其他張。
    mode='normal'：/關所有工單（結單以外）；mode='done'：/關閉結單工單（只處理「結單但還沒真正關好」的）。"""
    done_mode = (mode == 'done'); label = '關閉結單工單' if done_mode else '關所有工單'
    guild = i.guild; gid = str(guild.id)
    batch = ids[:CLOSE_ALL_MAX_PER_RUN]; rest = len(ids) - len(batch)
    closed = 0; skipped = 0; stuck_now = []; move_fail = []; rename_fail = 0; errors = []
    _CLOSE_ALL_RUNNING.add(gid); _VISIBLE_MEMBER_CACHE.clear()
    try:
        for n, cid in enumerate(batch, 1):
            ch = guild.get_channel(int(cid))
            if not isinstance(ch, discord.TextChannel): skipped += 1; continue
            st = ticket_current_status(ch)
            if done_mode:
                if not name_is_done(ch) or not await ticket_not_really_closed(guild, ch): skipped += 1; continue
            elif st == '結單' or (status != CLOSE_ALL_ALL and st != status): skipped += 1; continue
            try:
                res = await (close_ticket_channel(guild, ch, i.user, label, True, keep_order=True) if done_mode else close_ticket_channel(guild, ch, i.user, label, True))
                if done_mode:
                    _VISIBLE_MEMBER_CACHE.clear(); left = await ticket_not_really_closed(guild, ch)
                    if left:
                        _CLOSE_STUCK[str(ch.id)] = (datetime.now().timestamp(), '、'.join(left)); stuck_now.append(ch.mention)
                    else: _CLOSE_STUCK.pop(str(ch.id), None)
                closed += 1
                if not res['moved']: move_fail.append(f'#{res["ticket_number"]}')
                if not res['rename_ok']: rename_fail += 1
            except Exception as e:
                traceback.print_exc(); errors.append(f'{ch.mention}：{type(e).__name__} {str(e)[:60]}')
            if n % 10 == 0:
                try: await i.edit_original_response(content=f'⏳ 關閉中… {n}/{len(batch)}')
                except Exception: pass
    finally:
        _CLOSE_ALL_RUNNING.discard(gid)
    lines = [f'✅ **{label}完成**' + ('' if done_mode else f'（{status}）') + f'：已關閉 **{closed}** 張']
    if skipped: lines.append(f'• 略過 {skipped} 張（預覽後狀態已變、已經關好、或頻道已不存在）')
    if move_fail: lines.append(f'• 沒能移入結單分類：{"、".join(move_fail[:15])}' + ('…' if len(move_fail) > 15 else '') + '（請確認已設定結單分類、機器人有管理頻道權限）')
    if rename_fail: lines.append(f'• {rename_fail} 張改名失敗或被 Discord 限速（同頻道 10 分鐘內只能改名 2 次），名稱沒更新')
    if errors: lines.append('• 出錯：' + '；'.join(errors[:5]))
    if stuck_now: lines.append(f'• 有 {len(stuck_now)} 張處理後仍沒關好（{"、".join(stuck_now[:8])}{"…" if len(stuck_now) > 8 else ""}），6 小時內不會再列入，免得佔掉名額。多半是**結單分類已滿 50 個頻道**，請清掉舊工單或新增結單分類後再執行。')
    if rest: lines.append(f'⚠️ 還有 **{rest}** 張沒處理（每次最多 {CLOSE_ALL_MAX_PER_RUN} 張），請再執行一次 `/{label}`。')
    try: await post_channel_log(guild, 'action_log_channel_id', f'👑 **{label}**｜' + ('' if done_mode else f'{status}｜') + f'關閉 {closed} 張｜{i.user.mention}')
    except Exception as e: print(f'[CLOSE_ALL] 寫入紀錄失敗｜error={e!r}')
    msg = '\n'.join(lines)[:1900]
    try: await i.edit_original_response(content=msg, view=None)
    except Exception:
        try: await i.channel.send(i.user.mention + '\n' + msg, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as e: print(f'[CLOSE_ALL] 回報結果失敗｜error={e!r}')
    return {'closed': closed, 'skipped': skipped, 'rest': rest, 'errors': errors}

class CloseAllConfirmView(discord.ui.View):
    def __init__(self, operator_id, status, ids, mode='normal'):
        super().__init__(timeout=180)
        self.operator_id = operator_id; self.status = status; self.ids = list(ids); self.mode = mode; self.started = False
        yes = discord.ui.Button(label=f'✅ 確認關閉 {len(self.ids)} 張', style=discord.ButtonStyle.danger); yes.callback = self.on_yes; self.add_item(yes)
        no = discord.ui.Button(label='取消', style=discord.ButtonStyle.secondary); no.callback = self.on_no; self.add_item(no)
    async def interaction_check(self, i):
        if i.user.id != self.operator_id:
            await i.response.send_message('這個確認只有發出指令的人能按。', ephemeral=True); return False
        return True
    async def on_yes(self, i):
        if not strict_admin(i.user): return await deny(i, True)
        if self.started: return await i.response.send_message('已經在處理了。', ephemeral=True)
        if str(i.guild.id) in _CLOSE_ALL_RUNNING: return await i.response.send_message('⏳ 已經有一個批量關閉在跑了，請等它結束。', ephemeral=True)
        self.started = True
        await i.response.edit_message(content=f'⏳ 開始關閉 {min(len(self.ids), CLOSE_ALL_MAX_PER_RUN)} 張工單…', view=None)
        await run_close_all(i, self.status, self.ids, self.mode)
    async def on_no(self, i):
        await i.response.edit_message(content='已取消，沒有關閉任何工單。', view=None)

@bot.tree.command(name='關所有工單', description='一次關閉很多張工單（可選要關哪個狀態）：移除客人權限、結單、移入結單分類')
@app_commands.describe(狀態='只關這個狀態的工單；選「全部」＝結單以外的全部')
@app_commands.choices(狀態=choices([CLOSE_ALL_ALL, CLOSE_ALL_NO_ORDER] + [s for s in STATUSES if s != '結單'] + ['待倒']))
async def close_all_tickets(i: discord.Interaction, 狀態: str):
    if not strict_admin(i.user): return await deny(i, True)
    await i.response.defer(ephemeral=True)
    if str(i.guild.id) in _CLOSE_ALL_RUNNING:
        return await i.followup.send('⏳ 已經有一個批量關閉在跑了，請等它結束。', ephemeral=True)
    found = find_tickets_to_close(i.guild, 狀態)
    if not found:
        return await i.followup.send(f'沒有符合的工單（狀態：{狀態}）。結單的工單不會被列入。', ephemeral=True)
    counts = {}
    for _ch, st in found: counts[st] = counts.get(st, 0) + 1
    lines = [f'⚠️ **要關閉 {len(found)} 張工單**（{狀態}）']
    if 狀態 == CLOSE_ALL_ALL: lines.append('各狀態：' + '、'.join(f'{k} {v}' for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    lines += [f'• {ch.mention}（{st}）' for ch, st in found[:12]]
    if len(found) > 12: lines.append(f'…還有 {len(found) - 12} 張')
    lines.append('\n每張都會：🔒 移除客人的存取權 → 改成「結單」→ 📁 移入結單分類。**客人會看不到工單。**')
    if len(found) > CLOSE_ALL_MAX_PER_RUN: lines.append(f'一次最多處理 {CLOSE_ALL_MAX_PER_RUN} 張，剩下的再執行一次。')
    await i.followup.send('\n'.join(lines)[:1900], view=CloseAllConfirmView(i.user.id, 狀態, [ch.id for ch, _ in found]), ephemeral=True)

@bot.tree.command(name='關閉結單工單', description='把「已經結單、但客人還看得到或還沒進結單分類」的工單真正關閉（店長等級）')
async def close_done_tickets(i: discord.Interaction):
    if not strict_admin(i.user): return await deny(i, True)
    await i.response.defer(ephemeral=True)
    if str(i.guild.id) in _CLOSE_ALL_RUNNING:
        return await i.followup.send('⏳ 已經有一個批量關閉在跑了，請等它結束。', ephemeral=True)
    found = await find_done_tickets_to_close(i.guild)
    stuck_n = sum(1 for c in list(_CLOSE_STUCK) if _stuck_active(c))
    if not found:
        return await i.followup.send('沒有需要處理的工單：所有結單的工單，客人都已看不到、也都在結單分類裡了。' + (f'\n（另有 {stuck_n} 張先前試過仍關不好、暫時略過，請檢查結單分類是否已滿。）' if stuck_n else ''), ephemeral=True)
    lines = [f'⚠️ **要關閉 {len(found)} 張「已結單」的工單**']
    lines += [f'• {ch.mention}（{why}）' for ch, why in found[:12]]
    if len(found) > 12: lines.append(f'…還有 {len(found) - 12} 張')
    lines.append('\n每張都會：🔒 移除客人的存取權 → 📁 移入結單分類。**客人會看不到工單。**訂單狀態與結單時間不變。')
    if stuck_n: lines.append(f'（另有 {stuck_n} 張先前試過仍關不好，已暫時略過。）')
    if len(found) > CLOSE_ALL_MAX_PER_RUN: lines.append(f'一次最多處理 {CLOSE_ALL_MAX_PER_RUN} 張，剩下的再執行一次。')
    await i.followup.send('\n'.join(lines)[:1900], view=CloseAllConfirmView(i.user.id, CLOSE_DONE_STATUS, [ch.id for ch, _ in found], mode='done'), ephemeral=True)

@bot.tree.command(name='關閉工單',description='關閉目前工單、移除客人存取權並移入結單分類')
async def close_ticket_command(i):
    await _close_ticket_core(i,'關閉工單',revoke=True)

@bot.tree.command(name='查詢工單',description='查詢工單')
@app_commands.describe(狀態='可不填',商品='可不填')
@app_commands.choices(狀態=choices(STATUSES),商品=choices(QUERY_PRODUCT_CHOICES))
async def query_orders(i,狀態:str|None=None,商品:str|None=None):
    if not admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    sql='SELECT * FROM orders WHERE guild_id=?'; params=[str(i.guild.id)]
    if 狀態: sql+=' AND status=?'; params.append(狀態)
    if 商品: sql+=' AND product=?'; params.append(商品)
    sql+=' ORDER BY id DESC LIMIT 50'; rows=q(sql,params,True)
    if not rows: return await i.followup.send('📋 目前沒有符合條件的工單。',ephemeral=True)
    lines=[]
    for o in rows:
        try: ch=i.guild.get_channel(int(o['channel_id']))
        except (TypeError,ValueError): ch=None
        link=ch.jump_url if isinstance(ch,discord.TextChannel) else ''
        lines.append(f'{status_icon(o["status"])} `#{o["ticket_no"]}` {o["product"]} × {o["quantity"]}｜**{o["status"]}**'+(f'｜[前往]({link})' if link else ''))
    # Embed 描述上限 4096 字：依長度分頁，一則訊息最多 10 張卡片，其餘再另外送出。
    pages=[]; cur=''
    for ln in lines:
        if len(cur)+1+len(ln)>3800: pages.append(cur); cur=ln
        else: cur=(cur+'\n'+ln) if cur else ln
    if cur: pages.append(cur)
    title=f'📋 工單查詢｜共 {len(rows)} 筆'+(f'｜{狀態}' if 狀態 else '')+(f'｜{商品}' if 商品 else '')
    embeds=[]
    for n,p in enumerate(pages,1):
        e=discord.Embed(title=title if n==1 else f'{title}（續 {n}）',description=p,color=status_color(狀態) if 狀態 else UI_COLOR['brand'])
        if n==len(pages): brand_footer(e,'最多顯示最近 50 筆')
        embeds.append(e)
    for k in range(0,len(embeds),10): await i.followup.send(embeds=embeds[k:k+10],ephemeral=True)

@bot.tree.command(name='今日統計',description='查看今天的訂單統計與金額')
async def today_stats(i):
    if not admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('Asia/Taipei'); today=datetime.now(tz).date()
    # 只撈「今天」的訂單（台北時間 0 點～隔天 0 點，轉成 UTC 字串比對，可走 guild_id+created_at 索引），
    # 並在資料庫裡直接分組加總；以前會把全部訂單載入記憶體再逐筆轉時區。
    start=datetime(today.year,today.month,today.day,tzinfo=tz).astimezone(timezone.utc).isoformat()
    end=(datetime(today.year,today.month,today.day,tzinfo=tz)+timedelta(days=1)).astimezone(timezone.utc).isoformat()
    rows=q('SELECT status,COUNT(*) AS n,COALESCE(SUM(total_price),0) AS amt,COALESCE(SUM(CASE WHEN paid_at IS NOT NULL THEN total_price ELSE 0 END),0) AS paid_amt FROM orders WHERE guild_id=? AND created_at>=? AND created_at<? GROUP BY status',(str(i.guild.id),start,end),True)
    counts={r['status']:int(r['n']) for r in rows}
    total=sum(int(r['amt']) for r in rows); n_all=sum(counts.values())
    # 「結單」可能是未付款就被關閉的單，只算有付款時間的；其他進行中狀態本來就已付款。
    paid=sum((int(r['paid_amt']) if r['status']=='結單' else int(r['amt'])) for r in rows if r['status'] not in ('待付款','已取消'))
    e=discord.Embed(title=f'📊 今日訂單統計｜{today.strftime("%Y/%m/%d")}',color=UI_COLOR['brand'],timestamp=datetime.now(timezone.utc))
    e.add_field(name='🧾 訂單數',value=f'**{n_all}** 筆',inline=True)
    e.add_field(name='💰 訂單總額',value=f'**NT${total:,}**',inline=True)
    e.add_field(name='💳 付款後流程',value=f'**NT${paid:,}**',inline=True)
    order_=['待付款','待確認付款','待排單','待交貨','處理中','待收貨','有爭議','結單','已取消']
    detail=[f'{status_icon(st)} {st}：**{counts[st]}** 筆' for st in order_ if counts.get(st)]
    e.add_field(name='📌 各狀態',value='\n'.join(detail) if detail else '今天還沒有訂單',inline=False)
    brand_footer(e,'台北時間')
    await i.followup.send(embed=e,ephemeral=True)

BALANCE_MAX_PER_OP=10_000_000   # 單次加／扣上限，擋掉多打幾個 0 的手誤

def _ts(iso):
    """資料庫的 UTC 時間 → Discord 時間戳（每個人看到自己時區的時間）。"""
    try: return f'<t:{int(datetime.fromisoformat(iso).timestamp())}:f>'
    except Exception: return str(iso or '')[:16]

def _balance_of(user_id):
    r=q('SELECT balance FROM balances WHERE user_id=?',(str(user_id),),True)
    return int(r[0]['balance']) if r else 0

@bot.tree.command(name='查詢餘額',description='查詢自己的或指定會員的餘額')
@app_commands.describe(會員='店長可指定其他會員；一般會員留空')
async def balance(i,會員:discord.Member|None=None):
    await i.response.defer(ephemeral=True)
    if 會員 and 會員.id!=i.user.id and not strict_admin(i.user):
        return await i.followup.send('❌ 只有店長可以查詢其他會員的餘額；不選會員就是查自己的。',ephemeral=True)
    target=會員 or i.user
    b=_balance_of(target.id)
    e=discord.Embed(title='💰 會員餘額',description=f'{target.mention}',color=UI_COLOR['money'],timestamp=datetime.now(timezone.utc))
    e.add_field(name='目前餘額',value=f'**NT${b:,}**',inline=True)
    last=q('SELECT * FROM balance_logs WHERE user_id=? ORDER BY id DESC LIMIT 1',(str(target.id),),True)
    if last: e.add_field(name='最近一次異動',value=f'{_ts(last[0]["created_at"])}\n{last[0]["action"]} NT${abs(int(last[0]["amount"])):,}',inline=True)
    brand_footer(e,'完整紀錄請用 /餘額紀錄')
    await i.followup.send(embed=e,ephemeral=True)

async def balance_change(i,會員,金額,action,備註=''):
    if not strict_admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    if 金額<=0: return await i.followup.send('❌ 金額必須大於 0。',ephemeral=True)
    if getattr(會員,'bot',False): return await i.followup.send('❌ 不能對機器人帳號加扣餘額。',ephemeral=True)
    note=(備註 or '').strip()[:200]
    # 餘額是金錢資料，必須在同一個 transaction 內讀取＋更新＋寫入流水，避免兩個管理員同時操作造成覆蓋。
    async with lock:
        with db:
            cur=db.cursor()
            cur.execute('SELECT balance FROM balances WHERE user_id=?',(str(會員.id),))
            row=cur.fetchone(); old=int(row['balance']) if row else 0
            new=old+金額 if action=='增加' else old-金額
            if new<0:
                return await i.followup.send(f'❌ {會員.mention} 餘額不足，無法扣除 NT${金額:,}（目前餘額 NT${old:,}）。',ephemeral=True)
            cur.execute('INSERT INTO balances(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance',(str(會員.id),new))
            cur.execute('INSERT INTO balance_logs(guild_id,user_id,operator_id,amount,balance_after,action,created_at,note) VALUES(?,?,?,?,?,?,?,?)',(str(i.guild.id),str(會員.id),str(i.user.id),金額,new,action,now(),note))
    await post_channel_log(i.guild,'action_log_channel_id',f'💰 **餘額{action}**｜{會員.mention}｜NT${金額:,}｜操作人：{i.user.mention}｜餘額：NT${new:,}'+(f'｜備註：{note}' if note else ''))
    e=discord.Embed(title=f'✅ 餘額已{action}',description=f'{會員.mention}',color=UI_COLOR['ok'] if action=='增加' else UI_COLOR['warn'],timestamp=datetime.now(timezone.utc))
    e.add_field(name='異動前',value=f'NT${old:,}',inline=True); e.add_field(name=f'{action}',value=f'NT${金額:,}',inline=True); e.add_field(name='異動後',value=f'**NT${new:,}**',inline=True)
    if note: e.add_field(name='備註',value=note,inline=False)
    brand_footer(e)
    await i.followup.send(embed=e,ephemeral=True)

@bot.tree.command(name='增加餘額',description='增加會員餘額')
@app_commands.describe(會員='要加餘額的會員',金額=f'1～{BALANCE_MAX_PER_OP:,}',備註='原因，例如「退款 #0289」（選填，會寫進紀錄）')
async def add_balance(i,會員:discord.Member,金額:app_commands.Range[int,1,BALANCE_MAX_PER_OP],備註:str=''): await balance_change(i,會員,金額,'增加',備註)
@bot.tree.command(name='扣除餘額',description='扣除會員餘額')
@app_commands.describe(會員='要扣餘額的會員',金額=f'1～{BALANCE_MAX_PER_OP:,}',備註='原因，例如「購買 100M不死×2」（選填，會寫進紀錄）')
async def sub_balance(i,會員:discord.Member,金額:app_commands.Range[int,1,BALANCE_MAX_PER_OP],備註:str=''): await balance_change(i,會員,金額,'扣除',備註)

@bot.tree.command(name='餘額紀錄',description='查看餘額異動紀錄（會員看自己的；店長可看指定會員）')
@app_commands.describe(會員='店長可指定其他會員；一般會員留空',筆數='顯示最近幾筆（預設 10，最多 25）')
async def balance_history(i,會員:discord.Member|None=None,筆數:app_commands.Range[int,1,25]=10):
    await i.response.defer(ephemeral=True)
    is_boss=strict_admin(i.user)
    if 會員 and 會員.id!=i.user.id and not is_boss:
        return await i.followup.send('❌ 只有店長可以查看其他會員的餘額紀錄；不選會員就是看自己的。',ephemeral=True)
    target=會員 or i.user
    rows=q('SELECT * FROM balance_logs WHERE user_id=? ORDER BY id DESC LIMIT ?',(str(target.id),int(筆數)),True)
    e=discord.Embed(title='🧾 餘額紀錄',description=target.mention,color=UI_COLOR['money'],timestamp=datetime.now(timezone.utc))
    e.add_field(name='目前餘額',value=f'**NT${_balance_of(target.id):,}**',inline=False)
    if not rows:
        e.add_field(name='異動紀錄',value='還沒有任何餘額異動。',inline=False)
    else:
        lines=[]
        for r in rows:
            amt=int(r['amount']); minus=(r['action'] in ('扣除','訂單付款')) or amt<0
            sign='➖' if minus else '➕'
            line=f'{_ts(r["created_at"])}｜{sign} {r["action"]} NT${abs(amt):,} → 餘額 NT${int(r["balance_after"]):,}'
            if r['note']: line+=f'\n　└ {clip(r["note"],80)}'
            if is_boss and str(r['operator_id']).isdigit(): line+=f'｜操作：<@{r["operator_id"]}>'
            lines.append(line)
        e.add_field(name=f'最近 {len(rows)} 筆',value=clip('\n'.join(lines),1000),inline=False)
    brand_footer(e)
    await i.followup.send(embed=e,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name='餘額總覽',description='店長用：所有會員餘額總額與餘額最高的會員')
async def balance_overview(i):
    if not strict_admin(i.user): return await deny(i, True)
    await i.response.defer(ephemeral=True)
    t=q('SELECT COUNT(*) AS n,COALESCE(SUM(balance),0) AS s FROM balances WHERE balance>0',(),True)[0]
    top=q('SELECT user_id,balance FROM balances WHERE balance>0 ORDER BY balance DESC LIMIT 10',(),True)
    e=discord.Embed(title='💰 餘額總覽',color=UI_COLOR['money'],timestamp=datetime.now(timezone.utc))
    e.add_field(name='有餘額的會員',value=f'**{int(t["n"]):,}** 人',inline=True)
    e.add_field(name='餘額總額（店家需負擔）',value=f'**NT${int(t["s"]):,}**',inline=True)
    e.add_field(name='餘額最高 10 位',value=clip('\n'.join(f'{n}. <@{r["user_id"]}>｜NT${int(r["balance"]):,}' for n,r in enumerate(top,1)) or '目前沒有人有餘額。',1000),inline=False)
    brand_footer(e)
    await i.followup.send(embed=e,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name='購買相關資訊',description='查看購買相關資訊')
async def buy_info(i):
    text=get(i.guild.id,'buy_info','請先確認商品、數量、價格及交易規則後再付款。\n如有疑問，請於付款前提出。')
    await i.response.send_message(text,ephemeral=True)

@bot.tree.command(name='設定購買資訊',description='設定購買相關資訊')
async def set_buy_info(i,內容:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'buy_info',內容); await i.response.send_message('✅ 購買相關資訊已更新。',ephemeral=True)

@bot.tree.command(name='設定管理員',description='設定管理員身分組')
async def set_admin_role(i,身分組:discord.Role):
    if not i.user.guild_permissions.administrator: return await deny(i)
    if 身分組.id==i.guild.id:
        return await i.response.send_message('❌ 不能把 @everyone 設成管理員身分組（那等於所有人都是管理員）。請選店長／管理員專用的身分組。',ephemeral=True)
    setv(i.guild.id,'admin_role_id',身分組.id); await i.response.send_message(f'✅ 已設定管理員身分組為 {身分組.mention}。',ephemeral=True)

@bot.tree.command(name='檢查權限', description='查看某位成員在機器人眼中是不是管理員、為什麼（客人能用管理指令時用這個查）')
@app_commands.describe(會員='要檢查的成員（不選就是自己）')
async def check_permission(i: discord.Interaction, 會員: discord.Member | None = None):
    if not admin(i.user): return await deny(i)
    await i.response.send_message(permission_report(會員 or i.user, i.guild)[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name='設定店長公告',description='設定暫停接單時，客人會多看到的公告')
async def set_rest_text(i,內容:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'rest_text',內容); await i.response.send_message('✅ 公告已更新。客人在「暫停接單」期間想下單時會看到。',ephemeral=True)


@bot.tree.command(name='開啟無限制開單',description='店長開啟後，同一位客人可以同時建立多張未結單工單')
async def enable_unlimited_ticket_opening(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'unlimited_ticket_opening','1')
    await i.response.send_message('✅ 已開啟「無限制開單」。客人可以同時建立多張未結單工單。',ephemeral=True)

@bot.tree.command(name='關閉無限制開單',description='關閉同一位客人同時建立多張未結單工單的功能')
async def disable_unlimited_ticket_opening(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'unlimited_ticket_opening','0')
    await i.response.send_message('✅ 已關閉「無限制開單」。恢復每位客人只能保留一張未結單工單。',ephemeral=True)

@bot.tree.command(name='無限制開單狀態',description='查看目前是否開啟無限制開單')
async def unlimited_ticket_opening_status(i):
    if not admin(i.user): return await deny(i)
    enabled=get(i.guild.id,'unlimited_ticket_opening','0') == '1'
    await i.response.send_message(f"📋 無限制開單目前為：{'🟢 開啟' if enabled else '🔴 關閉'}",ephemeral=True)

@bot.tree.command(name='設定開單身分組',description='設定可以查看所有工單的指定身分組')
async def set_ticket_role(i,身分組:discord.Role):
    if not strict_admin(i.user): return await deny(i, True)
    if 身分組 == i.guild.default_role: return await i.response.send_message('❌ 不能把 @everyone 設為工單身分組，否則所有會員都可能看見工單。',ephemeral=True)
    setv(i.guild.id,'ticket_staff_role_id',身分組.id); await i.response.send_message(f'✅ 工單指定身分組已設定為 {身分組.mention}。',ephemeral=True)

@bot.tree.command(name='設定客服身分組',description='設定客服身分組：能看到所有新工單，客人按完成付款時會 @ 這個身分組')
async def set_support_role(i,身分組:discord.Role):
    if not strict_admin(i.user): return await deny(i, True)
    if 身分組 == i.guild.default_role: return await i.response.send_message('❌ 不能把 @everyone 設為客服身分組。',ephemeral=True)
    setv(i.guild.id,'support_role_id',身分組.id)
    tip='' if 身分組.mentionable or i.guild.me.guild_permissions.mention_everyone else '\n⚠️ 這個身分組目前**不可被提及**，機器人又沒有「提及所有身分組」權限，@ 不會真的通知到人。請到身分組設定開啟「允許任何人 @提及此身分組」，或給機器人該權限。'
    await i.response.send_message(f'✅ 客服身分組已設定為 {身分組.mention}。\n之後新建工單會讓這個身分組看得到；客人按「完成付款」時會 @ 它。'+tip,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name='建立開單面板',description='在目前頻道發送開單面板（單一開單按鈕）')
async def create_open_panel(i):
    if not admin(i.user): return await deny(i)
    if not isinstance(i.channel,discord.TextChannel): return await i.response.send_message('❌ 請在文字頻道使用。',ephemeral=True)
    await i.response.defer(ephemeral=True)
    e=discord.Embed(title='🎫 開單中心',description='按下方按鈕即可建立專屬工單 👇\n\n▸ 機器人會引導您選擇商品、數量並付款\n▸ 有任何問題，客服人員會在工單內協助您',color=UI_COLOR['brand'])
    brand_footer(e,'按下按鈕即可建立專屬工單')
    try:
        await i.channel.send(embed=e,view=OpenTicketView())
    except discord.HTTPException as ex:
        return await i.followup.send(f'❌ 面板發送失敗：`{str(ex)[:150]}`（請確認機器人有此頻道的發送訊息／嵌入連結權限）',ephemeral=True)
    await i.followup.send('✅ 開單面板已發送。先前發過的舊面板（兩顆按鈕的那種）仍然有效，按下後一樣直接開單。',ephemeral=True)

@bot.tree.command(name='設定工單訊息',description='設定新工單建立後的第一則訊息')
async def set_ticket_welcome(i,內容:str):
    if not admin(i.user): return await deny(i)
    if len(內容) > 2000:
        return await i.response.send_message('❌ 工單第一則訊息最多 2000 字，請縮短後再設定。',ephemeral=True)
    setv(i.guild.id,'ticket_welcome',內容)
    await i.response.send_message('✅ 新工單第一則訊息已更新。',ephemeral=True)

# ===== 免洗帳號：上架／庫存／發號 =====
ISSUE_TEXT_TARGETS = ['全部'] + LOGIN_TYPES

def _issue_text_key(target):
    return 'account_issue_text' if target == '全部' else f'account_issue_text:{target}'

def _issue_text_for(guild_id, login_type):
    """發號時一起發出的自訂訊息：該登入方式專屬 → 全部通用 → 沒有。"""
    return (get(guild_id, _issue_text_key(login_type), '') or get(guild_id, _issue_text_key('全部'), '') or '').strip()

def _fill_issue_text(text, buyer_id, product, qty, login_type):
    mention = f'<@{buyer_id}>' if str(buyer_id or '').isdigit() else '客人'
    return (text.replace('{客人}', mention).replace('{商品}', str(product)).replace('{數量}', str(qty))
                .replace('{登入方式}', str(login_type or '')).replace('\\n', '\n'))

def _format_account(content, idx=None):
    """帳號內容以「----」、「｜」或換行分隔欄位時，發號時分行顯示；否則原樣顯示。"""
    parts = [x.strip() for x in re.split(r'\s*(?:----|｜)\s*|\s*\n\s*', content or '') if x.strip()]
    body = '\n'.join(parts) if len(parts) > 1 else (content or '').strip()
    return (f'【第 {idx} 組】\n' if idx else '') + body

def _account_md(content, idx=None):
    """發號用：一組帳號的所有行放在同一個程式碼框裡（一鍵複製整組）。
    欄位原本用 ---- 或 ｜ 分隔的，會拆成一行一個欄位；原本就分行的照原樣保留。"""
    lines = [x.strip() for x in re.split(r'\s*(?:----|｜)\s*|\s*\n\s*', content or '') if x.strip()] or [(content or '').strip()]
    body = '\n'.join(lines).replace('```', "'''")   # 內容裡若有三個反引號，會把外框提早關掉，所以換掉
    return (f'**【第 {idx} 組】**\n' if idx else '') + f'```\n{body}\n```'

def _chunk_messages(header, blocks, footer='', limit=1900, raw=False):
    """帳號分段送出；自訂訊息(footer)接在最後一段後面，放不下才另開一則。"""
    msgs = []; cur = header
    for b in blocks:
        piece = f'\n{b}' if raw else f'\n```\n{b}\n```'
        if len(cur) + len(piece) > limit:
            msgs.append(cur); cur = piece.lstrip('\n')
        else:
            cur += piece
    if footer:
        extra = '\n\n' + footer
        if len(cur) + len(extra) <= 2000: cur += extra
        else:
            msgs.append(cur); cur = footer[:2000]
    msgs.append(cur)
    return msgs

def _split_long(text, limit=1990):
    """單則訊息超過 2000 字時依行切開（帳號內容很長才會用到）。"""
    if len(text) <= limit: return [text]
    out = []; cur = ''
    for ln in text.split('\n'):
        while len(ln) > limit:
            if cur: out.append(cur); cur = ''
            out.append(ln[:limit]); ln = ln[limit:]
        if len(cur) + len(ln) + 1 > limit: out.append(cur); cur = ln
        else: cur = (cur + '\n' + ln) if cur else ln
    if cur: out.append(cur)
    return out

def _issue_messages(header, blocks, footer=''):
    """發號訊息：先一則標題（@客人），之後每一組帳號各自一則（欄位用程式碼區塊框起來，方便點一下複製），最後才是自訂說明。"""
    msgs = [header]
    for b in blocks: msgs.extend(_split_long(b))
    if footer: msgs.extend(_split_long(footer[:2000]))
    return msgs

def _decode_text(raw):
    for enc in ('utf-8-sig', 'cp950', 'gbk'):
        try: return raw.decode(enc)
        except UnicodeDecodeError: continue
    return raw.decode('utf-8', errors='ignore')

# 登入方式可以不指定：存成空字串，在庫存與發號時視為「未指定」。
LOGIN_NONE = ''
POOL_TYPES = LOGIN_TYPES + [LOGIN_NONE]

def _lt_label(lt):
    return lt or '未指定登入'

# 分隔線：連續 3 個以上的長橫線（— – ─ ━）可以放在任何位置；
# ASCII 的 - = _ 只有「單獨一行」（3 個以上）或連續 6 個以上才算，避免跟欄位分隔 ---- 搞混。
_ACC_SEP_RE = re.compile(r'(?:[—–─━]{3,}|-{6,}|^[ \t]*[-=_＝＿]{3,}[ \t]*$)', re.M)

def split_account_blocks(text):
    """把貼上的文字拆成一組一組帳號。
    有分隔線 → 依分隔線切開（每一段是一組，段內換行保留）；
    沒有分隔線 → 一行一組。"""
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n')
    if _ACC_SEP_RE.search(text):
        parts = _ACC_SEP_RE.split(text)
        return [p.strip() for p in (re.sub(r'\n\s*\n+', '\n', x).strip() for x in parts) if p.strip()]
    return [ln.strip() for ln in text.split('\n') if ln.strip()]

@bot.tree.command(name='上架帳號',description='把免洗帳號放進庫存：可一次貼很多組（用 ————— 分開，機器人自動拆成一隻一隻）或上傳 .txt')
@app_commands.describe(商品='這批帳號是哪個幣號規格',登入方式='（選填）Garena 或 Google 登入；不選＝未指定',內容='帳號內容。多組之間用 ————— 分開；欄位用 ---- 分隔，例如：帳號：a@x----密碼：123',檔案='.txt 檔：有 ————— 就依分隔線拆，沒有就一行一組')
@app_commands.choices(商品=choices(COIN_PRODUCTS),登入方式=choices(LOGIN_TYPES))
async def add_accounts(i:discord.Interaction,商品:str,登入方式:str|None=None,內容:str='',檔案:discord.Attachment|None=None):
    if not admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    lt=登入方式 or LOGIN_NONE
    items=[]
    if str(內容).strip(): items += split_account_blocks(str(內容))
    if 檔案 is not None:
        if getattr(檔案,'size',0)>1_000_000: return await i.followup.send('❌ 檔案太大（上限 1 MB）。',ephemeral=True)
        try: text=_decode_text(await 檔案.read())
        except Exception as e: return await i.followup.send(f'❌ 讀取檔案失敗：`{str(e)[:150]}`',ephemeral=True)
        items += split_account_blocks(text)
    if not items: return await i.followup.send('❌ 請輸入帳號內容，或上傳 .txt 檔。',ephemeral=True)
    gid=str(i.guild.id); added=0; dup=0; seen=set()
    async with lock:
        with db:
            cur=db.cursor()
            for c in items:
                if c in seen: dup+=1; continue
                seen.add(c)
                # 同一組帳號內容不論登入方式／規格，只能存在一筆（避免同一個帳號被賣兩次）。
                cur.execute('SELECT 1 FROM accounts WHERE guild_id=? AND content=? LIMIT 1',(gid,c))
                if cur.fetchone(): dup+=1; continue
                cur.execute('INSERT INTO accounts(guild_id,product,content,login_type,created_at) VALUES(?,?,?,?,?)',(gid,商品,c,lt,now())); added+=1
            if added: cur.execute('UPDATE products SET stock=stock+? WHERE product=?',(added,商品))
    avail=q("SELECT COUNT(*) AS n FROM accounts WHERE guild_id=? AND product=? AND login_type=? AND status='available'",(gid,商品,lt),True)[0]['n']
    await i.followup.send(f'✅ 已拆成 **{len(items)}** 組，上架 **{added}** 組｜{_lt_label(lt)}｜{商品}'+(f'\n⚠️ 重複略過 {dup} 組' if dup else '')+f'\n📦 這個組合目前可發：**{avail}** 組（現貨數已同步增加）',ephemeral=True)

@bot.tree.command(name='帳號庫存',description='查看各幣號規格、各登入方式可發的免洗帳號數量')
async def account_stock(i:discord.Interaction):
    if not admin(i.user): return await deny(i)
    gid=str(i.guild.id)
    rows=q("SELECT product,login_type,SUM(status='available') AS a,SUM(status='issued') AS d FROM accounts WHERE guild_id=? GROUP BY product,login_type",(gid,),True)
    by={(r['product'],r['login_type']):r for r in rows}
    th=low_stock_threshold(i.guild.id)
    lines=[]
    for p in COIN_PRODUCTS:
        parts=[]
        for lt in POOL_TYPES:
            r=by.get((p,lt)); a=int(r['a'] or 0) if r else 0; d=int(r['d'] or 0) if r else 0
            if lt==LOGIN_NONE and not (a or d): continue
            parts.append(f'{"⚠️ " if th>0 and a<=th else ""}{_lt_label(lt)} 可發 **{a}**（已發 {d}）')
        lines.append(f'**{p}**\n　'+'｜'.join(parts))
    legacy=sum(int(r['a'] or 0) for r in rows if r['login_type'] not in POOL_TYPES or r['product'] not in COIN_PRODUCTS)
    if legacy: lines.append(f'⚠️ 另有 {legacy} 組資料的登入方式或規格不在目前清單內')
    if th>0: lines.append(f'🔔 低庫存提醒：剩 **{th}** 組以下會提醒（`/設定低庫存提醒` 可調整，0＝關閉）')
    e=discord.Embed(title='🗂️ 免洗帳號庫存',description='\n\n'.join(lines) or '目前沒有庫存資料',color=UI_COLOR['info'],timestamp=datetime.now(timezone.utc)); brand_footer(e)
    await i.response.send_message(embed=e,ephemeral=True)

@bot.tree.command(name='帳號庫存總表',description='把庫存裡所有可發的帳號直接列給管理人員（只有你看得到；不會標成已發，也不用發貨）')
@app_commands.describe(商品='（選填）只看某個幣號規格；不選＝全部')
@app_commands.choices(商品=choices(COIN_PRODUCTS))
async def account_stock_dump(i:discord.Interaction,商品:str|None=None):
    # 帳號是明文，只開放店長等級（與匯出資料庫相同），客服身分組不能用。
    if not strict_admin(i.user): return await deny(i, True)
    await i.response.defer(ephemeral=True)
    gid=str(i.guild.id)
    sql="SELECT product,login_type,content FROM accounts WHERE guild_id=? AND status='available'"+(" AND product=?" if 商品 else "")+" ORDER BY product,login_type,id"
    rows=q(sql,(gid,商品) if 商品 else (gid,),True)
    if not rows: return await i.followup.send('📭 目前沒有可發的帳號庫存。',ephemeral=True)
    groups={}
    for r in rows: groups.setdefault((r['product'],r['login_type']),[]).append(r['content'])
    order=lambda k:(COIN_PRODUCTS.index(k[0]) if k[0] in COIN_PRODUCTS else 99, POOL_TYPES.index(k[1]) if k[1] in POOL_TYPES else 99)
    keys=sorted(groups,key=order)
    # 檔案：組與組之間用 ————— 分開，之後可以直接拿去 /上架帳號 重新上架。
    file_lines=[]
    for k in keys:
        file_lines.append(f'【{k[0]}｜{_lt_label(k[1])}】共 {len(groups[k])} 組')
        file_lines.append('\n—————\n'.join(groups[k])); file_lines.append('')
    data='\n'.join(file_lines).encode('utf-8')
    total=len(rows)
    head=f'🗂️ **帳號庫存總表**｜共 **{total}** 組可發（只有你看得到，沒有標成已發）'
    msgs=[head]
    for k in keys:
        blocks=[_account_md(c,n) for n,c in enumerate(groups[k],1)]
        msgs.append(f'**【{k[0]}｜{_lt_label(k[1])}】共 {len(groups[k])} 組**')
        for b in blocks: msgs.extend(_split_long(b))
    fname=f'帳號庫存總表_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")}.txt'
    f=discord.File(io.BytesIO(data),filename=fname)
    if len(msgs)>12:
        return await i.followup.send(head+f'\n組數較多，改用檔案提供（每個區塊有標題行，組與組用 `—————` 分開；要貼回 `/上架帳號` 請先刪掉標題行）。',file=f,ephemeral=True)
    await i.followup.send(msgs[0],file=f,ephemeral=True)
    for m in msgs[1:]:
        await i.followup.send(m,ephemeral=True)
        if len(msgs)>6: await asyncio.sleep(0.3)

LOW_STOCK_DEFAULT=3
def low_stock_threshold(guild_id):
    try: return max(0,int(get(guild_id,'low_stock_threshold',str(LOW_STOCK_DEFAULT))))
    except (TypeError,ValueError): return LOW_STOCK_DEFAULT

async def maybe_alert_low_stock(guild,product,login_type,left,issued):
    """發號後，若某規格＋登入方式的庫存剛降到門檻以下（或用完），在操作紀錄頻道提醒店長補貨。
    只在「剛跨過門檻」或「歸零」時提醒一次，不會每發一次就洗版。"""
    th=low_stock_threshold(guild.id)
    if th<=0: return
    before=int(left)+int(issued)
    if not ((int(left)<=th<before) or int(left)==0): return
    rid=str(get(guild.id,'admin_role_id','')).strip()
    ping=f'<@&{rid}> ' if rid.isdigit() else ''
    head='🚨 **免洗帳號用完了**' if int(left)==0 else '⚠️ **免洗帳號庫存偏低**'
    await post_channel_log(guild,'action_log_channel_id',f'{ping}{head}｜{_lt_label(login_type)}｜{product}｜剩 **{int(left)}** 組（提醒門檻 {th}）。請盡快用 `/上架帳號` 補貨。')

async def issue_accounts_core(guild, ch, order, login_type, qty, product, operator, force_status=False, force_dup=False, qty_given=True):
    """發免洗帳號給工單頻道。回傳 (成功與否, 給操作者看的訊息)。/發號 與「🚚 發貨」按鈕共用。"""
    if not product or not qty:
        return False,'❌ 這張工單沒有訂單紀錄，請指定「商品」與「數量」。'
    if product not in COIN_PRODUCTS:
        return False,f'❌ 「{product}」不是需要發帳號的幣號規格，請用「商品」指定 {"、".join(COIN_PRODUCTS)}。'
    if not 1<=int(qty)<=100: return False,'❌ 數量請輸入 1～100。'
    qty=int(qty); gid=str(guild.id)
    if order:
        if not force_status and order['status'] in ('待付款','待確認付款','已取消'):
            return False,f'❌ 這張訂單目前是「{order["status"]}」，還不能發號。確定要發的話，把「強制」設為 True。'
        if order['status']=='已取消':
            return False,'❌ 這張訂單已取消，不能發貨。'
    async with lock:
        with db:
            cur=db.cursor()
            # 重複發貨檢查放在鎖內：兩個人同時按「發貨」時，後面那個會看到已發過而被擋下。
            if order and not force_dup and not qty_given:
                cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE order_id=? AND status='issued'",(order['id'],))
                done=cur.fetchone()['n']
                if done>=int(order['quantity']):
                    return False,f'❌ 這張訂單已發過 **{done}** 組（訂單數量 {order["quantity"]}）。要補發請指定「數量」，或把「強制」設為 True。'
            cur.execute("SELECT id,content FROM accounts WHERE guild_id=? AND product=? AND login_type=? AND status='available' ORDER BY id LIMIT ?",(gid,product,login_type,qty))
            picked=cur.fetchall()
            if len(picked)<qty:
                return False,f'❌ 庫存不足：{_lt_label(login_type)}｜{product} 目前只有 **{len(picked)}** 組可發，需要 {qty} 組。請先用 `/上架帳號` 補貨，或改選另一種登入方式。'
            for r in picked:
                # 只有仍是 available 的才能被標成已發，確保每組帳號只會發出一次。
                cur.execute("UPDATE accounts SET status='issued',order_id=?,channel_id=?,issued_by=?,issued_at=? WHERE id=? AND status='available'",(order['id'] if order else None,str(ch.id),str(operator.id),now(),r['id']))
                if cur.rowcount!=1: raise sqlite3.IntegrityError('帳號已被發出，請重新執行 /發號')
    buyer_id=str(order['buyer_id']) if order and order['buyer_id'] else ''
    header=(f'<@{buyer_id}>\n' if buyer_id.isdigit() else '')+f'# 📦 帳號已發送\n**'+(f'{login_type}｜' if login_type else '')+f'{product} × {qty}**'
    blocks=[_account_md(r['content'],n if qty>1 else None) for n,r in enumerate(picked,1)]
    custom=_issue_text_for(guild.id,login_type)
    footer=_fill_issue_text(custom,buyer_id,product,qty,login_type) if custom else ''
    sent=0
    try:
        _msgs=_issue_messages(header,blocks,footer)
        for n_,msg in enumerate(_msgs):
            await ch.send(msg); sent+=1
            if len(_msgs)>6 and n_<len(_msgs)-1: await asyncio.sleep(0.3)   # 大量發號時稍微放慢，避免撞到 Discord 速率限制
    except discord.HTTPException as e:
        if sent==0:
            ids=[r['id'] for r in picked]
            q(f"UPDATE accounts SET status='available',order_id=NULL,channel_id=NULL,issued_by=NULL,issued_at=NULL WHERE id IN ({','.join('?'*len(ids))})",ids)
            return False,f'❌ 發送失敗，帳號已放回庫存：`{str(e)[:150]}`'
        return False,f'⚠️ 只送出部分訊息就失敗了（{str(e)[:120]}）。帳號已標為已發，請用 `/發號紀錄` 查看並手動補貼。'
    note=''
    if order:
        try:
            log_order(order['id'],guild.id,operator.id,'發號',f'{_lt_label(login_type)}｜{product} × {qty}')
            if order['status']=='待確認付款':
                log_order(order['id'],guild.id,operator.id,'確認付款','發貨時一併確認（已有付款明細）')
            if order['status'] not in ('待收貨','結單'):
                q('UPDATE orders SET status=? WHERE id=?',('待收貨',order['id']))
                log_order(order['id'],guild.id,operator.id,'發號後狀態',f'{order["status"]} → 待收貨')
                await rename_order_channel(q('SELECT * FROM orders WHERE id=?',(order['id'],),True)[0],guild,'發號')
                await status_announce(q('SELECT * FROM orders WHERE id=?',(order['id'],),True)[0],guild)
                note='\n訂單已更新為「待收貨」。'
        except Exception as e:
            traceback.print_exc(); note=f'\n⚠️ 帳號已發出，但更新訂單狀態時出錯：`{str(e)[:120]}`'
    left=q("SELECT COUNT(*) AS n FROM accounts WHERE guild_id=? AND product=? AND login_type=? AND status='available'",(gid,product,login_type),True)[0]['n']
    try: await post_channel_log(guild,'action_log_channel_id',f'📦 **發號**｜{ch.mention}｜{_lt_label(login_type)}｜{product} × {qty}｜剩餘 {left}｜{operator.mention}')
    except Exception: pass
    try: await maybe_alert_low_stock(guild,product,login_type,left,qty)
    except Exception: traceback.print_exc()
    return True,f'✅ 已發送 **{qty}** 組（{_lt_label(login_type)}｜{product}）。剩餘可發：**{left}** 組。'+('' if custom else '\n💡 還沒設定發號訊息，可用 `/設定發號訊息` 加上固定說明。')+note

@bot.tree.command(name='發號',description='從庫存取出免洗帳號發給這張工單的客人（每組帳號只會發一次）')
@app_commands.describe(登入方式='（選填）要發 Garena 還是 Google 登入的帳號；不選就從庫存足夠的（含未指定登入）中隨機挑一種',數量='要發幾組；不填就依這張訂單的數量',商品='不填就依這張訂單的商品',強制='訂單尚未付款或已發過號時，仍要發')
@app_commands.choices(登入方式=choices(LOGIN_TYPES),商品=choices(COIN_PRODUCTS))
async def issue_accounts(i:discord.Interaction,登入方式:str|None=None,數量:int|None=None,商品:str|None=None,強制:bool=False):
    if not admin(i.user): return await deny(i)
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return await i.response.send_message('❌ 請在工單頻道內使用 `/發號`。',ephemeral=True)
    await i.response.defer(ephemeral=True)
    rows=q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(ch.id),),True)
    order=rows[0] if rows else None
    product=商品 or (order['product'] if order else None)
    qty=數量 if 數量 is not None else (int(order['quantity']) if order else None)
    if 登入方式 is None:
        # 沒選登入方式：從庫存足夠的登入方式中隨機挑一種。
        if not product or not qty or product not in COIN_PRODUCTS or not 1<=int(qty)<=100:
            # 參數有問題就交給 core 回報對應的錯誤訊息（隨便帶一種登入方式即可）。
            _ok,msg=await issue_accounts_core(i.guild,ch,order,LOGIN_TYPES[0],qty,product,i.user,force_status=強制,force_dup=強制,qty_given=數量 is not None)
            return await i.followup.send(msg,ephemeral=True)
        stock_by={lt:q("SELECT COUNT(*) AS n FROM accounts WHERE guild_id=? AND product=? AND login_type=? AND status='available'",(str(i.guild.id),product,lt),True)[0]['n'] for lt in POOL_TYPES}
        ok_types=[lt for lt,n in stock_by.items() if n>=int(qty)]
        if not ok_types:
            detail='、'.join(f'{_lt_label(lt)} {n} 組' for lt,n in stock_by.items() if n or lt!=LOGIN_NONE)
            return await i.followup.send(f'❌ 庫存不足：{product} 需要 **{int(qty)}** 組，但單一登入方式都不夠（{detail}）。請先 `/上架帳號` 補貨，或指定登入方式分批發。',ephemeral=True)
        登入方式=random.choice(ok_types)
    _ok,msg=await issue_accounts_core(i.guild,ch,order,登入方式,qty,product,i.user,force_status=強制,force_dup=強制,qty_given=數量 is not None)
    await i.followup.send(msg,ephemeral=True)

@bot.tree.command(name='設定發號訊息',description='設定發號時，跟帳號一起發出去的說明訊息（可依登入方式分開）')
@app_commands.describe(類型='套用到哪種登入方式；「全部」＝通用，個別設定會優先',內容='可用 {客人} {商品} {數量} {登入方式}；用 \\n 換行；留空＝查看目前設定；輸入「清除」＝刪除')
@app_commands.choices(類型=choices(ISSUE_TEXT_TARGETS))
async def set_issue_text(i:discord.Interaction,類型:str,內容:str=''):
    if not admin(i.user): return await deny(i)
    key=_issue_text_key(類型); text=str(內容).strip()
    if not text:
        cur=get(i.guild.id,key,'')
        return await i.response.send_message(f'📝 **{類型}** 發號訊息：\n'+(f'```\n{cur}\n```' if cur else '（尚未設定）')+'\n可用代號：`{客人}` `{商品}` `{數量}` `{登入方式}`',ephemeral=True)
    if text=='清除':
        setv(i.guild.id,key,'')
        return await i.response.send_message(f'🗑️ 已清除「{類型}」的發號訊息。',ephemeral=True)
    if len(text)>1500: return await i.response.send_message('❌ 發號訊息最多 1500 字。',ephemeral=True)
    setv(i.guild.id,key,text)
    preview=_fill_issue_text(text,i.user.id,COIN_PRODUCTS[0],1,類型 if 類型!='全部' else LOGIN_TYPES[0])
    await i.response.send_message(f'✅ 已設定「{類型}」發號訊息。預覽：\n{preview}',ephemeral=True)

@bot.tree.command(name='設定工單客人',description='更正這張工單的客人（工單被記成別人時使用）')
@app_commands.describe(客人='這張工單真正的客人')
async def set_ticket_buyer(i:discord.Interaction,客人:discord.Member):
    if not admin(i.user): return await deny(i)
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return await i.response.send_message('❌ 請在工單頻道內使用。',ephemeral=True)
    if 客人.bot: return await i.response.send_message('❌ 客人不能是機器人。',ephemeral=True)
    await i.response.defer(ephemeral=True)
    rec=ticket_record(ch)
    if not rec:
        no=ticket_no(ch.name)
        if not no: return await i.followup.send('❌ 這個頻道不是工單（名稱裡找不到工單號）。',ephemeral=True)
        remember(ch,no,客人.id,客人.display_name)
    else:
        q('UPDATE ticket_channels SET buyer_id=?,buyer_name=? WHERE channel_id=?',(str(客人.id),客人.display_name,str(ch.id)))
    # 還沒付款（沒動過餘額）且還在進行中的訂單，一併改成正確的客人；已用餘額付款的訂單不動，避免退款對不上人。
    fixed=0
    for o in q("SELECT id,buyer_id,balance_paid FROM orders WHERE channel_id=? AND status NOT IN ('結單','已取消')",(str(ch.id),),True):
        if str(o['buyer_id'])!=str(客人.id) and int(o['balance_paid'] or 0)==0:
            q('UPDATE orders SET buyer_id=? WHERE id=?',(str(客人.id),o['id'])); log_order(o['id'],i.guild.id,i.user.id,'更正客人',f'{o["buyer_id"]} → {客人.id}'); fixed+=1
    for o in q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(ch.id),),True):
        try: await status_announce(o,i.guild)
        except Exception: traceback.print_exc()
    await post_channel_log(i.guild,'action_log_channel_id',f'🛠️ **更正工單客人**｜{ch.mention}｜{客人.mention}｜{i.user.mention}')
    await i.followup.send(f'✅ 已把這張工單的客人設為 {客人.mention}。'+(f'\n同時更正了 **{fixed}** 筆進行中的訂單。' if fixed else ''),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name='訂單操作紀錄',description='查看這張工單訂單的操作紀錄（誰在什麼時候確認付款、發號、改狀態）')
@app_commands.describe(筆數='顯示最近幾筆（預設 15，最多 30）')
async def order_audit(i:discord.Interaction,筆數:app_commands.Range[int,1,30]=15):
    if not admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    r=q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(i.channel.id),),True)
    if not r: return await i.followup.send('📋 這個頻道目前沒有訂單紀錄。',ephemeral=True)
    o=r[0]
    rows=q('SELECT * FROM order_logs WHERE order_id=? ORDER BY id DESC LIMIT ?',(o['id'],int(筆數)),True)
    lines=[]
    for x in rows:
        who=f'<@{x["operator_id"]}>' if str(x['operator_id'] or '').isdigit() and x['operator_id']!='0' else '系統'
        line=f'{_ts(x["created_at"])}｜**{x["action"]}**｜{who}'
        if x['detail']: line+=f'\n　└ {clip(x["detail"],120)}'
        lines.append(line)
    e=discord.Embed(title=f'🧾 訂單操作紀錄｜#{o["ticket_no"]}',description=f'{o["product"]} × {o["quantity"]}｜NT${int(o["total_price"]):,}｜目前：**{o["status"]}**',color=UI_COLOR['info'],timestamp=datetime.now(timezone.utc))
    e.add_field(name=f'最近 {len(rows)} 筆',value=clip('\n'.join(lines) or '還沒有操作紀錄。',1000),inline=False)
    brand_footer(e)
    await i.followup.send(embed=e,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

@bot.tree.command(name='發號紀錄',description='查看這張工單已發出的帳號（只有你看得到）')
async def account_issue_log(i:discord.Interaction):
    if not admin(i.user): return await deny(i)
    ch=i.channel
    rows=q("SELECT * FROM accounts WHERE channel_id=? AND status='issued' ORDER BY issued_at,id",(str(ch.id),),True)
    if not rows: return await i.response.send_message('📭 這張工單還沒有發過帳號。',ephemeral=True)
    blocks=[f'{r["login_type"] or "未分類"}｜{r["product"]}｜{r["issued_at"][:16]}\n{_format_account(r["content"])}' for r in rows]
    msgs=_chunk_messages(f'📦 **發號紀錄**｜共 {len(rows)} 組',blocks)
    await i.response.send_message(msgs[0],ephemeral=True)
    for m_ in msgs[1:]: await i.followup.send(m_,ephemeral=True)

@bot.tree.command(name='停止發送面板',description='新工單不再自動發送「購買服務」面板（仍會發歡迎訊息與關閉工單按鈕）')
async def stop_purchase_panel(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'purchase_panel_enabled','0')
    await i.response.send_message('⏹️ 已停止發送購買服務面板。新工單只會有歡迎訊息與關閉工單按鈕；已存在的面板不受影響。用 `/開啟發送面板` 可恢復。',ephemeral=True)

@bot.tree.command(name='開啟發送面板',description='新工單恢復自動發送「購買服務」面板')
async def start_purchase_panel(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'purchase_panel_enabled','1')
    await i.response.send_message('▶️ 已恢復發送購買服務面板。',ephemeral=True)

@bot.tree.command(name='設定開單分類',description='設定新工單建立在哪個分類')
async def set_ticket_category(i,分類:discord.CategoryChannel):
    if not admin(i.user): return await deny(i)
    if 分類.id in _closed_category_ids(i.guild):
        return await i.response.send_message(f'❌ {分類.mention} 目前被設為「結單分類」（/設定服務工單分類），不能同時當開單分類。請選另一個分類，例如「幣號工單」。',ephemeral=True)
    setv(i.guild.id,'ticket_category_id',分類.id)
    await i.response.send_message(f'✅ 開單分類已設定為 {分類.mention}。之後新開的工單都會建在這個分類（滿 50 個會自動開下一個）。'+('\nℹ️ 這是「服務中」分類：新單一開就算服務中。`/自動排序工單號` 不會動「服務中」裡的工單；其他分類的工單照常排序進「幣號工單」那一串，不受開單分類影響。' if IN_SERVICE_CATEGORY_KEYWORD in (分類.name or '') else ''),ephemeral=True)

class ManualRouteView(discord.ui.View):
    """手動分流：管理員自己選工單與目標分類；不改工單名稱、不改訂單狀態。"""
    def __init__(self, guild, max_tickets=1):
        super().__init__(timeout=300)
        self.guild=guild
        self.guild_id=guild.id
        self.max_tickets=max(1,min(int(max_tickets),25))
        self.target_category_id=None
        self.ticket_ids=[]

        self.target_select=discord.ui.ChannelSelect(
            placeholder='① 選擇要移入的目標分類',
            channel_types=[discord.ChannelType.category],
            min_values=1,max_values=1,row=0
        )
        self.target_select.callback=self.on_target
        self.add_item(self.target_select)

        self.ticket_select=discord.ui.ChannelSelect(
            placeholder='② 選擇要移動的工單（可複選）',
            channel_types=[discord.ChannelType.text],
            min_values=1,max_values=self.max_tickets,row=1
        )
        self.ticket_select.callback=self.on_tickets
        self.add_item(self.ticket_select)

        self.move_button=discord.ui.Button(label='🚚 執行手動分流',style=discord.ButtonStyle.success,row=2)
        self.move_button.callback=self.execute_move
        self.add_item(self.move_button)

    async def interaction_check(self,i):
        if not admin(i.user):
            await i.response.send_message('❌ 只有管理員可以操作手動分流。',ephemeral=True)
            return False
        return True

    async def on_target(self,i):
        vals=self.target_select.values or []
        cat=vals[0] if vals else None
        self.target_category_id=getattr(cat,'id',None)
        await i.response.send_message(f'📁 目標分類已選擇：**{getattr(cat,"name","未知分類")}**。',ephemeral=True)

    async def on_tickets(self,i):
        self.ticket_ids=[getattr(ch,'id',None) for ch in (self.ticket_select.values or []) if getattr(ch,'id',None)]
        names=[f'`{getattr(ch,"name","未知工單")}`' for ch in (self.ticket_select.values or [])]
        await i.response.send_message('🎫 已選工單：'+('、'.join(names) if names else '無'),ephemeral=True)

    async def execute_move(self,i):
        await i.response.defer(ephemeral=True)
        if not self.target_category_id:
            return await i.followup.send('❌ 請先選擇目標分類。',ephemeral=True)
        if not self.ticket_ids:
            return await i.followup.send('❌ 請先選擇至少一張工單。',ephemeral=True)
        category=self.guild.get_channel(int(self.target_category_id))
        if not isinstance(category,discord.CategoryChannel):
            return await i.followup.send('❌ 目標分類不存在，請重新開啟手動分流。',ephemeral=True)
        if len(category.channels)+len(self.ticket_ids)>50:
            # 實際會扣掉原本就在該分類的頻道，避免誤判。
            current=sum(1 for cid in self.ticket_ids if self.guild.get_channel(int(cid)) is not None and getattr(self.guild.get_channel(int(cid)),'category_id',None)==category.id)
            if len(category.channels)-current+len(self.ticket_ids)>50:
                return await i.followup.send(f'❌ 目標分類最多只能有 50 個頻道，目前無法一次移入這批工單。',ephemeral=True)
        moved=[]; failed=[]; skipped=[]
        for cid in self.ticket_ids:
            ch=self.guild.get_channel(int(cid))
            if not isinstance(ch,discord.TextChannel):
                failed.append(str(cid)); continue
            no=_channel_ticket_number(ch)
            if no is None:
                skipped.append(ch.name); continue
            if ch.category_id==category.id:
                skipped.append(ch.name); continue
            try:
                await ch.edit(category=category,reason=f'CT小舖 手動分流｜操作者 {i.user.id}')
                moved.append(ch.name)
            except (discord.Forbidden,discord.HTTPException) as e:
                print(f'[MANUAL_ROUTE] 移動失敗｜channel={ch.id}｜target={category.id}｜error={e!r}')
                failed.append(f'{ch.name}：{e}')
            await asyncio.sleep(0.08)
        lines=[f'🚚 **手動分流完成**｜目標：{category.mention}',f'成功：**{len(moved)}**',f'跳過：**{len(skipped)}**',f'失敗：**{len(failed)}**']
        if moved: lines.append('\n已移動：'+ '、'.join(f'`{x}`' for x in moved[:20]))
        if skipped: lines.append('\n已跳過：'+ '、'.join(f'`{x}`' for x in skipped[:20]))
        if failed: lines.append('\n失敗：'+ '、'.join(f'`{x}`' for x in failed[:10]))
        await i.followup.send('\n'.join(lines),ephemeral=True)
        self.stop()

@bot.tree.command(name='手動操作分流',description='手動選擇工單並移動到指定分類，不改工單名稱')
async def manual_route_command(i):
    await i.response.defer(ephemeral=True)
    if not admin(i.user): return await deny(i)
    await i.followup.send('🗂️ **手動操作分流**\n請先選擇「目標分類」，再選擇要移動的工單，最後按「執行手動分流」。\n\n📌 此操作只移動頻道，不會自動改名、不會改訂單狀態。',view=ManualRouteView(i.guild,1),ephemeral=True)

@bot.tree.command(name='批量移動工單',description='手動選擇多張工單並一次移動到指定分類')
async def batch_move_tickets_command(i):
    await i.response.defer(ephemeral=True)
    if not admin(i.user): return await deny(i)
    await i.followup.send('📦 **批量移動工單**\n一次最多手動選 25 張工單。先選目標分類，再選工單，最後按「執行手動分流」。\n\n📌 不會改工單名稱、不會改訂單狀態。',view=ManualRouteView(i.guild,25),ephemeral=True)

@bot.tree.command(name='設定服務工單分類',description='設定結單後的幣號分類與代肝分類')
async def set_service_ticket_categories(i, 幣號分類:discord.CategoryChannel, 代肝分類:discord.CategoryChannel):
    if not admin(i.user): return await deny(i)
    if 幣號分類.id == 代肝分類.id:
        return await i.response.send_message('❌ 幣號與代肝不能使用同一個分類。',ephemeral=True)
    _base=_base_ticket_category(i.guild)
    if _base is not None and _base.id in (幣號分類.id,代肝分類.id):
        return await i.response.send_message(f'❌ {_base.mention} 是目前的「開單分類」，不能同時當結單分類。結單分類請選「幣號結單」「代肝結單」這類分類。',ephemeral=True)
    setv(i.guild.id,'service_category_coin_id',str(幣號分類.id))
    setv(i.guild.id,'service_category_boost_id',str(代肝分類.id))
    await i.response.send_message(f'✅ 已設定固定服務分類。\n🪙 幣號 → {幣號分類.mention}\n🛠️ 代肝 → {代肝分類.mention}\n\n之後只有按「關閉工單」或「完成訂單」時才會自動移入對應分類；開單、選服務、付款、改名、改狀態都不會自動移動頻道。',ephemeral=True)

@bot.tree.command(name='建立服務工單分類',description='自動建立「幣號結單」與「代肝結單」兩個固定分類')
async def create_service_ticket_categories(i):
    await i.response.defer(ephemeral=True)
    if not admin(i.user): return await deny(i)
    me=i.guild.me
    if me is None or not me.guild_permissions.manage_channels:
        return await i.followup.send('❌ 機器人需要「管理頻道」權限才能建立分類。',ephemeral=True)
    result=[]
    template_id=get(i.guild.id,'ticket_category_id','')
    template=i.guild.get_channel(int(template_id)) if template_id.isdigit() else None
    overwrites=template.overwrites if isinstance(template,discord.CategoryChannel) else None
    for service,key,label in [('幣號','service_category_coin_id','幣號結單'),('代肝','service_category_boost_id','代肝結單')]:
        cat=await ensure_service_category(i.guild,service)
        if not isinstance(cat,discord.CategoryChannel):
            existing=discord.utils.get(i.guild.categories,name=label) or discord.utils.get(i.guild.categories,name=label.replace('結單','工單'))
            if existing is not None:
                cat=existing
            else:
                try:
                    kwargs={'name':label,'reason':'CT小舖建立固定服務工單分類'}
                    if overwrites is not None: kwargs['overwrites']=overwrites
                    cat=await i.guild.create_category(**kwargs)
                except discord.Forbidden:
                    return await i.followup.send('❌ 建立分類失敗：機器人沒有管理頻道權限。',ephemeral=True)
                except discord.HTTPException as e:
                    return await i.followup.send(f'❌ 建立分類失敗：{e}',ephemeral=True)
            setv(i.guild.id,key,str(cat.id))
        result.append(f'{"🪙" if service=="幣號" else "🛠️"} {cat.mention}')
    await i.followup.send('✅ 固定服務分類已建立／設定。\n'+'\n'.join(result),ephemeral=True)

@bot.tree.command(name='查看服務工單分類',description='查看目前結單後的幣號／代肝分類')
async def show_service_ticket_categories(i):
    if not admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    coin=await ensure_service_category(i.guild,'幣號'); boost=await ensure_service_category(i.guild,'代肝')
    await i.followup.send(f'🪙 幣號結單分類：{coin.mention if coin else "❌ 尚未設定"}\n🛠️ 代肝結單分類：{boost.mention if boost else "❌ 尚未設定"}\n\n📌 開啟中的工單不移動；只有關閉工單／完成訂單時才自動歸類（改名、改狀態不會移動頻道）。',ephemeral=True)

@bot.tree.command(name='設定自動分類',description='設定頻道自動移動到指定分類並排序')
async def setup_auto_category(i):
    # Slash command 的第一件事就是 ACK，連權限判斷都放到 ACK 後，避免 DB/Member 快取造成 3 秒逾時。
    try:
        await i.response.defer(ephemeral=True)
        if not admin(i.user):
            return await deny(i)
        view=AutoCategorySetupView(i.guild, operator_id=i.user.id)
        view.guild=i.guild
        cfg=_auto_category_config(i.guild.id)
        status=f'\n\n目前總開關：{"🟢 開啟" if cfg["enabled"] else "🔴 關閉"}'
        await i.edit_original_response(content=view._render(i.guild)+status,view=view)
    except discord.NotFound as e:
        print(f'[AUTO_CATEGORY_SETUP] Discord API 互動已過期／不存在｜guild={getattr(i.guild,"id",None)}｜error={e!r}')
    except Exception as e:
        print(f'[AUTO_CATEGORY_SETUP] 錯誤｜guild={getattr(i.guild,"id",None)}｜error={e!r}')
        traceback.print_exc()
        try: await i.followup.send(f'❌ 自動分類設定面板開啟失敗：`{str(e)[:180]}`',ephemeral=True)
        except Exception: pass

@bot.tree.command(name='關閉自動分類',description='關閉頻道自動分類')
async def disable_auto_category(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'auto_category_enabled','0')
    await i.response.send_message('🔴 自動分類已關閉。已存在的頻道不會被移動。',ephemeral=True)

@bot.tree.command(name='開啟自動分類',description='重新開啟已設定的頻道自動分類')
async def enable_auto_category(i):
    if not admin(i.user): return await deny(i)
    cfg=_auto_category_config(i.guild.id)
    if not cfg['source_ids'] or not cfg['destination_id']:
        return await i.response.send_message('❌ 尚未設定自動分類。請先使用 `/設定自動分類`。',ephemeral=True)
    setv(i.guild.id,'auto_category_enabled','1')
    await i.response.send_message(f'🟢 自動分類已開啟。目標：<#{cfg["destination_id"]}>｜排序：{cfg["sort_mode"]}',ephemeral=True)

@bot.tree.command(name='立即整理分類',description='依目前自動分類設定立即整理來源分類內的頻道')
async def run_auto_category_now(i):
    await i.response.defer(ephemeral=True)
    if not admin(i.user): return await deny(i)
    cfg=_auto_category_config(i.guild.id)
    if not cfg['source_ids'] or not cfg['destination_id']:
        return await i.followup.send('❌ 尚未設定自動分類。請先使用 `/設定自動分類`。',ephemeral=True)
    moved,failed,err=await auto_categorize_channels(i.guild,cfg['source_ids'],cfg['destination_id'],cfg['sort_mode'])
    if err: return await i.followup.send(f'❌ 整理失敗：{err}',ephemeral=True)
    await i.followup.send(f'✅ 整理完成。\n已移動：{moved} 個頻道\n失敗：{failed} 個\n排序：{cfg["sort_mode"]}',ephemeral=True)


# 自動偵測工單
async def delayed(ch):
    """偵測外部／舊 Ticket Bot 建立的工單。每個頻道只允許一條偵測流程。"""
    if not isinstance(ch, discord.TextChannel): return
    key=str(ch.id)
    lk=DETECTION_LOCKS.setdefault(key, asyncio.Lock())
    if lk.locked(): return
    async with lk:
        try:
            await asyncio.sleep(1.0)
            current=ch.guild.get_channel(ch.id)
            if not isinstance(current, discord.TextChannel): return
            if ticket_record(current): return
            if not is_ticket_candidate(current): return

            no=ticket_no(current.name)
            if not no: return
            remember(current,no)

            # 舊／外部工單也不再自動發送交易／服務選擇面板。
            await asyncio.sleep(1.0)
            current=current.guild.get_channel(current.id)
            if not isinstance(current, discord.TextChannel): return
            if ticket_no(current.name) != no: return
            if is_ticket_candidate(current):
                try:
                    await current.edit(name=f'工單-{int(no):04d}', reason='CT小舖自動辨識工單號')
                except discord.HTTPException as e:
                    print(f'[TICKET_DETECT] 自動改名失敗｜channel={current.id}｜error={e!r}')
            # 自動偵測只統一名稱，不把開啟中的工單移到結單分類。
        finally:
            if DETECTION_LOCKS.get(key) is lk: DETECTION_LOCKS.pop(key,None)

_GAME_ACCOUNT_PREFIX=re.compile(r'^\s*(?:遊戲帳號|遊戲账号|帳號|账号|account)\s*[:：]\s*(.+)$',re.I|re.S)
_NOT_ACCOUNT_WORDS=('請問','請教','多久','幾天','幾點','什麼','怎麼','為什麼','可以嗎','好了嗎','嗎','謝謝','感謝','好的','了解','收到')

def extract_game_account(text):
    """從客人訊息判斷是不是遊戲帳號，回傳 (帳號, 是否明確標示)；看起來只是聊天就回傳 (None, False)。

    - 「帳號：xxx」「遊戲帳號：xxx」開頭 → 明確標示，一律採用（也可用來更正已收到的帳號）。
    - 否則必須是單行、不超過 100 字、不含問句或客套話，才當成帳號。
    """
    text=(text or '').strip()
    if not text: return None,False
    m=_GAME_ACCOUNT_PREFIX.match(text)
    if m:
        acc=m.group(1).strip()
        return (acc[:200],True) if acc else (None,False)
    if '\n' in text or len(text)>100: return None,False
    if '?' in text or '？' in text: return None,False
    if text.startswith(('/','@','http')) or any(w in text for w in _NOT_ACCOUNT_WORDS): return None,False
    if text.lower() in ('ok','okay','k','好','嗯','對','是'): return None,False
    return text,False

# ===== 工單訊息掃描：重複帳號偵測 =====
# 機器人會掃描所有工單裡的訊息，找出以「名稱：」或「郵件：」開頭的訊息，互相比對；
# 同一個帳號（郵件，沒有郵件就比名稱）出現第二次，就直接在那個工單回覆「偵測到重複」。
# 新訊息即時比對；以前的訊息用 /掃描重複帳號 補掃。
_ACCT_LABEL_RE = re.compile(r'^[ \t]*(名稱|名称|郵件|邮件)[ \t]*[:：][ \t]*(.*?)[ \t]*$')
_ACCT_CLEAN_RE = re.compile(r'```|\*\*|`|\|\|')
_ACCT_HEADER_RE = re.compile(r'^[ \t]*[【\[(（]?[ \t]*第[ \t]*\d+[ \t]*組[ \t]*[】\])）]?[ \t]*$')
_ACCT_SEP_LINE_RE = re.compile(r'^[ \t]*(?:[—–─━]{3,}|[-=_＝＿]{3,})[ \t]*$')

def extract_account_entries(text):
    """找出以「名稱：」或「郵件：」開頭的訊息裡的帳號，回傳 [(kind, 值), ...]。
    • 訊息（清掉程式碼框、粗體、「【第 1 組】」標題之後）的第一行必須以「名稱：」或「郵件：」開頭，聊天句子不算。
    • 同一組有郵件就用郵件比對，只有名稱才用名稱比對；「備用郵件：」不算郵件。
    • 一則訊息貼多組也可以：同一個標籤第二次出現，就視為下一組。"""
    lines = []
    for raw in _ACCT_CLEAN_RE.sub('', text or '').split('\n'):
        if raw.strip() and not _ACCT_HEADER_RE.match(raw): lines.append(raw)
    if not lines or not _ACCT_LABEL_RE.match(lines[0]): return []
    out, cur = [], {}
    def flush():
        if cur.get('mail'): out.append(('mail', cur['mail']))
        elif cur.get('name'): out.append(('name', cur['name']))
        cur.clear()
    for ln in lines:
        if _ACCT_SEP_LINE_RE.match(ln): flush(); continue
        m = _ACCT_LABEL_RE.match(ln)
        if not m: continue
        field = 'name' if m.group(1) in ('名稱', '名称') else 'mail'
        val = m.group(2).split()[0] if field == 'mail' and m.group(2).split() else m.group(2)
        if field in cur: flush()
        if val: cur[field] = val
    flush()
    return out

def account_fingerprint(guild_id, kind, value):
    return hashlib.sha256(f'{guild_id}\0{kind}\0{str(value).strip().lower()}'.encode('utf-8')).hexdigest()

def mask_account(value):
    v = str(value)
    if '@' in v:
        local, dom = v.split('@', 1); return f'{local[:2]}***@{dom}'
    return v[:2] + '***'

def ingest_account_message(guild_id, channel_id, ticket_no, author_id, message_id, sent_at, content):
    """把一則訊息裡的帳號記進紀錄（同一則訊息重複記不會出事）。回傳這次新增的紀錄 [(idx, kind, 值, 指紋)]。"""
    new = []
    for idx, (kind, value) in enumerate(extract_account_entries(content)):
        fp = account_fingerprint(guild_id, kind, value)
        cur = db.cursor()
        cur.execute('INSERT OR IGNORE INTO account_msg_log(message_id,idx,guild_id,channel_id,ticket_no,author_id,fingerprint,kind,shown,sent_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (int(message_id), idx, str(guild_id), str(channel_id), str(ticket_no or ''), str(author_id), fp, kind, mask_account(value), str(sent_at)))
        db.commit()
        if cur.rowcount: new.append((idx, kind, value, fp))
    return new

def earlier_account_rows(guild_id, fp, message_id, idx):
    """同一個帳號，比這則訊息更早出現過的紀錄（用訊息編號排先後，編號越小越早）。"""
    return q('SELECT * FROM account_msg_log WHERE guild_id=? AND fingerprint=? AND (message_id<? OR (message_id=? AND idx<?)) ORDER BY message_id,idx',
             (str(guild_id), fp, int(message_id), int(message_id), int(idx)), True)

def _dup_notice_text(items):
    lines = '\n'.join(f'• `{shown}`：這個帳號已經發送過（第 {times} 次）' for shown, times in items)
    return f'⚠️ **偵測到重複的帳號**\n{lines}\n請確認是不是發錯了。'

def _dup_log_text(ticket_no, items_with_prior):
    out = [f'⚠️ **偵測到重複帳號**｜#{ticket_no or "?"}']
    for shown, times, prior in items_with_prior:
        where = '、'.join(f'#{p["ticket_no"] or "?"}（{str(p["sent_at"])[:16]}）' for p in prior[-4:])
        out.append(f'`{shown}` 第 {times} 次｜之前：{where}')
    return '\n'.join(out)

_BG_TASKS = set()
def _run_in_background(coro):
    t = asyncio.ensure_future(coro); _BG_TASKS.add(t); t.add_done_callback(_BG_TASKS.discard)
    return t

async def _account_scan_live_safe(message):
    try: await account_scan_live(message)
    except Exception as e: print(f'[ACCOUNT_DUP] 即時偵測失敗｜channel={getattr(message.channel, "id", "?")}｜error={e!r}')

async def account_scan_live(message):
    """工單裡每一則新訊息都會呼叫：記錄帳號；同一個帳號第二次（含以後）出現，直接回覆「偵測到重複」。"""
    gid = message.guild.id
    try: ticket = _resolve_ticket_number(message.channel) or ''
    except Exception: ticket = ''
    new = ingest_account_message(gid, message.channel.id, ticket, message.author.id, message.id, str(message.created_at)[:16], message.content)
    items, log_items = [], []
    for idx, kind, value, fp in new:
        prior = earlier_account_rows(gid, fp, message.id, idx)
        if prior:
            items.append((mask_account(value), len(prior) + 1)); log_items.append((mask_account(value), len(prior) + 1, prior))
            q('UPDATE account_msg_log SET warned=1 WHERE message_id=? AND idx=?', (int(message.id), idx))
    if not items: return 0
    try: await message.reply(_dup_notice_text(items), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        try: await message.channel.send(_dup_notice_text(items), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e: print(f'[ACCOUNT_DUP] 發送提醒失敗｜channel={message.channel.id}｜error={e!r}')
    try: await post_channel_log(message.guild, 'action_log_channel_id', _dup_log_text(ticket, log_items))
    except Exception as e: print(f'[ACCOUNT_DUP] 寫入紀錄失敗｜error={e!r}')
    return len(items)

_ACCOUNT_SCAN_RUNNING = set()

async def scan_ticket_accounts(guild, announce=True, full=False):
    """掃描這個伺服器所有工單頻道的訊息（預設只掃上次之後的新訊息；full=True 從頭掃並清掉已被刪除的訊息的紀錄）。
    announce=True：在「第二次以後出現」的那則訊息回覆「偵測到重複」（每則只提醒一次）。回傳統計 dict。"""
    gid = str(guild.id)
    rep = {'channels': 0, 'messages': 0, 'skipped': 0, 'dups': [], 'announced': 0}
    chans = [c for c in (guild.get_channel(int(x)) for x in sorted(TICKET_CHANNEL_IDS, key=int)) if c is not None and isinstance(c, discord.TextChannel)]
    for ch in chans:
        st = None if full else q('SELECT last_message_id FROM account_scan_state WHERE channel_id=?', (str(ch.id),), True)
        after = discord.Object(id=int(st[0]['last_message_id'])) if st else None
        try: ticket = _resolve_ticket_number(ch) or ''
        except Exception: ticket = ''
        seen, last = set(), None
        try:
            async for m in ch.history(limit=None, after=after, oldest_first=True):
                rep['messages'] += 1; last = m.id
                if full: seen.add(int(m.id))
                if m.content: ingest_account_message(gid, ch.id, ticket, m.author.id, m.id, str(m.created_at)[:16], m.content)
        except discord.Forbidden:
            rep['skipped'] += 1; continue
        rep['channels'] += 1
        if last is not None: q('INSERT OR REPLACE INTO account_scan_state(channel_id,last_message_id) VALUES(?,?)', (str(ch.id), str(last)))
        if full:   # 從頭掃：被刪掉的訊息不該再算
            for r in q('SELECT DISTINCT message_id FROM account_msg_log WHERE channel_id=?', (str(ch.id),), True):
                if int(r['message_id']) not in seen: q('DELETE FROM account_msg_log WHERE message_id=?', (int(r['message_id']),))
    rows = q('''SELECT r.* FROM account_msg_log r WHERE r.guild_id=? AND EXISTS (SELECT 1 FROM account_msg_log e WHERE e.guild_id=r.guild_id AND e.fingerprint=r.fingerprint
                AND (e.message_id<r.message_id OR (e.message_id=r.message_id AND e.idx<r.idx))) ORDER BY r.message_id,r.idx''', (gid,), True)
    by_msg = {}
    for r in rows:
        prior = earlier_account_rows(gid, r['fingerprint'], r['message_id'], r['idx'])
        rep['dups'].append((r['shown'], len(prior) + 1, r['ticket_no'], bool(r['warned'])))
        if not r['warned']: by_msg.setdefault((r['channel_id'], r['message_id']), []).append((r, prior))
    if announce:
        for (cid, mid), pairs in by_msg.items():
            ch = guild.get_channel(int(cid))
            if ch is None: continue
            items = [(r['shown'], len(p) + 1) for r, p in pairs]
            try:
                try: await ch.get_partial_message(int(mid)).reply(_dup_notice_text(items), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                except discord.NotFound: await ch.send(_dup_notice_text(items), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                print(f'[ACCOUNT_DUP] 補掃提醒失敗｜channel={cid}｜error={e!r}'); continue
            for r, _p in pairs: q('UPDATE account_msg_log SET warned=1 WHERE message_id=? AND idx=?', (int(r['message_id']), int(r['idx'])))
            rep['announced'] += 1
    return rep

@bot.tree.command(name='掃描重複帳號', description='掃描所有工單的訊息，找「名稱：／郵件：」開頭而且重複的帳號，第二次出現的工單會直接提醒')
@app_commands.describe(公告='要不要直接在重複的工單發「偵測到重複」提醒（預設要；選否只回報給你，不發訊息）', 重新掃描='從頭掃全部訊息（預設只掃上次之後的新訊息）')
async def scan_dup_accounts(i: discord.Interaction, 公告: bool = True, 重新掃描: bool = False):
    if not admin(i.user): return await deny(i)
    gid = str(i.guild.id)
    if gid in _ACCOUNT_SCAN_RUNNING:
        return await i.response.send_message('⏳ 已經有一個掃描在跑了，請等它結束。', ephemeral=True)
    await i.response.defer(ephemeral=True)
    _ACCOUNT_SCAN_RUNNING.add(gid)
    try:
        n = len(TICKET_CHANNEL_IDS)
        await i.followup.send(f'🔍 開始掃描 {n} 個工單頻道…工單多的話要一點時間，結束後會回報。', ephemeral=True)
        rep = await scan_ticket_accounts(i.guild, announce=公告, full=重新掃描)
    except Exception as e:
        traceback.print_exc()
        return await i.followup.send(f'❌ 掃描中途出錯：`{str(e)[:150]}`', ephemeral=True)
    finally:
        _ACCOUNT_SCAN_RUNNING.discard(gid)
    lines = [f'✅ **掃描完成**：{rep["channels"]} 個工單、{rep["messages"]} 則訊息' + (f'（{rep["skipped"]} 個頻道沒有讀取權限，已略過）' if rep['skipped'] else '')]
    if not rep['dups']: lines.append('沒有發現重複的帳號。')
    else:
        lines.append(f'發現 **{len(rep["dups"])}** 則重複' + (f'，已在 {rep["announced"]} 則訊息回覆提醒' if 公告 else '（這次沒有發提醒）') + '：')
        lines += [f'• `{shown}` 第 {t} 次｜#{tk or "?"}{"（先前已提醒過）" if w else ""}' for shown, t, tk, w in rep['dups'][:15]]
        if len(rep['dups']) > 15: lines.append(f'…還有 {len(rep["dups"]) - 15} 則')
    msg = '\n'.join(lines)[:1900]
    try: await i.followup.send(msg, ephemeral=True)
    except discord.HTTPException:     # 掃太久、互動已過期：改發到目前頻道
        try: await i.channel.send(i.user.mention + '\n' + msg, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as e: print(f'[ACCOUNT_DUP] 回報掃描結果失敗｜error={e!r}')

@bot.event
async def on_raw_message_delete(payload):
    """訊息被刪掉，就不再算「發過」（例如發錯工單刪掉重發，不應該被當成重複）。"""
    try: q('DELETE FROM account_msg_log WHERE message_id=?', (int(payload.message_id),))
    except Exception as e: print(f'[ACCOUNT_DUP] 清除已刪訊息的紀錄失敗｜error={e!r}')

@bot.event
async def on_message(message):
    # 工單內的所有訊息（包含機器人 /發號 發的）：找「名稱：」「郵件：」開頭的帳號，第二次出現就直接提醒重複。
    if message.guild and message.content and isinstance(message.channel,discord.TextChannel) and str(message.channel.id) in TICKET_CHANNEL_IDS:
        _run_in_background(_account_scan_live_safe(message))   # 放背景跑：偵測慢或出錯，都不會拖住這則訊息後面的其他處理
    if message.author.bot or not isinstance(message.channel,discord.TextChannel):
        return

    # 自動算價：只偵測設定的店長本人訊息，不是 Slash Command。
    # 只偵測 100M不死*x、100M普通*x（x＝數量）與代肝額度（例如 200M）；管理員、店長、客服身分組的訊息才會觸發。
    # 可用 /關閉自動算價 關閉；預設開啟。
    if get(message.guild.id, 'auto_price_calc', '1') == '1':
        owner_id = get(message.guild.id, 'ticket_owner_id', '')
        sup=support_role(message.guild)
        is_calc_operator = admin(message.author) or (owner_id and str(message.author.id) == str(owner_id)) or bool(sup and sup in getattr(message.author,'roles',[]))
        if is_calc_operator:
            calc = parse_price_calculation(message.guild, message.content)
            if calc is not None:
                try:
                    await message.channel.send(calc['text'], allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as e:
                    print(f'[PRICE_CALC] 發送試算結果失敗｜guild={message.guild.id}｜channel={message.channel.id}｜error={e!r}')
                return

    # 付款圖片偵測不依賴 TICKET_CHANNEL_IDS 快取，避免工單剛建立／重啟 Bot 後漏偵測。
    # 客人可以先按「完成付款」再上傳，也可以先上傳圖片再按「完成付款」。
    proof=q("SELECT * FROM orders WHERE channel_id=? AND buyer_id=? AND status='待確認付款' ORDER BY id DESC LIMIT 1",(str(message.channel.id),str(message.author.id)),True)
    if proof and message.attachments:
        image_exts=('.png','.jpg','.jpeg','.webp','.gif','.bmp','.heic','.heif')
        image=next((a for a in message.attachments if (a.content_type or '').lower().startswith('image/') or (getattr(a,'filename','') or '').lower().endswith(image_exts)),None)
        if image:
            o=proof[0]
            q('UPDATE orders SET payment_proof_url=?,payment_proof_message_id=? WHERE id=?',(image.url,str(message.id),o['id']))
            log_order(o['id'],message.guild.id,message.author.id,'收到付款明細',image.url)
            rr=q('SELECT * FROM orders WHERE id=?',(o['id'],),True)[0]
            await message.channel.send(f'🧾 **已收到付款明細**\n付款明細已綁定至訂單 #{o["ticket_no"]}。\n⏳ 目前等待店長確認付款，請稍候。')
            await post_channel_log(message.guild,'order_log_channel_id',f'🧾 **收到付款明細**｜#{o["ticket_no"]}｜{message.author.mention}｜{image.url}')
            await status_announce(rr,message.guild)
            return

    # 普通頻道完全不碰 SQLite；只有已知工單才進入後續處理。
    if str(message.channel.id) not in TICKET_CHANNEL_IDS:
        await bot.process_commands(message)
        return
    # 代肝付款確認後，客人在工單直接傳遊戲帳號即可。
    # 只收「看起來像帳號」的訊息；聊天、問句（例如「請問多久？」）不會被當成帳號。
    # 客人也可以用「帳號：xxx」明確標示，或在收到後用同樣格式更正。
    rows=q("SELECT * FROM orders WHERE channel_id=? AND buyer_id=? AND service_type='代肝' AND status='待排單' ORDER BY id DESC LIMIT 1",(str(message.channel.id),str(message.author.id)),True)
    if rows and message.content.strip():
        o=rows[0]
        acc,explicit=extract_game_account(message.content)
        if acc and (o['game_account'] is None or explicit):
            is_fix=o['game_account'] is not None
            q('UPDATE orders SET game_account=? WHERE id=?',(acc,o['id']))
            log_order(o['id'],message.guild.id,message.author.id,'更正遊戲帳號' if is_fix else '收到遊戲帳號','客人於工單更正遊戲帳號' if is_fix else '客人於工單提供遊戲帳號')
            if is_fix:
                await message.channel.send('🎮 **已更正遊戲帳號**',allowed_mentions=discord.AllowedMentions.none())
            else:
                await message.channel.send('🎮 **已收到遊戲帳號**\n您的代肝訂單已排入處理。\n📋 排隊進度請至排隊網站查看。\n✏️ 如果帳號傳錯，請用「帳號：正確的帳號」重新傳一次。',allowed_mentions=discord.AllowedMentions.none())
            await post_channel_log(message.guild,'order_log_channel_id',f'🎮 **{"已更正遊戲帳號" if is_fix else "已收到遊戲帳號"}**｜#{o["ticket_no"]}｜{message.author.mention}')
            rr=q('SELECT * FROM orders WHERE id=?',(o['id'],),True)[0]
            await status_announce(rr,message.guild)
            return
    await bot.process_commands(message)

@bot.event
async def on_guild_channel_create(ch):
    if isinstance(ch,discord.TextChannel):
        asyncio.create_task(delayed(ch))
        asyncio.create_task(_auto_category_on_create(ch))

@bot.event
async def on_guild_channel_delete(ch):
    TICKET_CHANNEL_IDS.discard(str(ch.id))

@bot.event
async def on_guild_join(guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        synced=await bot.tree.sync(guild=guild)
        print(f'新伺服器同步成功：{guild.name} ({guild.id})，{len(synced)} 個指令')
    except Exception as e:
        print('guild join sync error',guild.id,repr(e))

async def cancel_unpaid_order(o, guild, who=None):
    """把「待付款」訂單改成已取消並退回預扣庫存。狀態已變就回傳 False。"""
    with db:
        cur=db.cursor(); cur.execute("UPDATE orders SET status='已取消' WHERE id=? AND status='待付款'",(o['id'],))
        if cur.rowcount!=1: return False
        if int(o['stock_reserved'] or 0)==1:
            cur.execute('UPDATE products SET stock=stock+? WHERE product=?',(int(o['quantity']),o['product']))
            cur.execute('UPDATE orders SET stock_reserved=0 WHERE id=?',(o['id'],))
    log_order(o['id'],guild.id,who,'自動取消','待付款逾時，訂單取消並釋出庫存')
    return True

async def _payment_tick():
    for g in bot.guilds:
        try: mins=int(get(g.id,'payment_reminder_minutes','15') or 15)
        except ValueError: mins=15
        if mins<=0: continue
        cutoff=datetime.now(timezone.utc)-timedelta(minutes=mins)
        rows=q("SELECT * FROM orders WHERE guild_id=? AND status='待付款' AND payment_reminder_sent=0",(str(g.id),),True)
        for o in rows:
            try: created=datetime.fromisoformat(o['created_at'])
            except Exception: continue
            if created>cutoff: continue
            try:
                ch=g.get_channel(int(o['channel_id']))
                q('UPDATE orders SET payment_reminder_sent=1 WHERE id=?',(o['id'],))
                log_order(o['id'],g.id,None,'付款逾時提醒',f'{mins} 分鐘未付款')
                await post_channel_log(g,'order_log_channel_id',f'⏰ **付款提醒**｜#{o["ticket_no"]}｜{o["product"]} × {o["quantity"]}｜NT${o["total_price"]:,}｜已超過 {mins} 分鐘未完成付款。')
                if isinstance(ch,discord.TextChannel):
                    try: auto=int(get(g.id,'auto_cancel_minutes','60') or 0)
                    except ValueError: auto=60
                    extra=f'\n⚠️ 若超過 **{auto} 分鐘**仍未付款，訂單會自動取消並釋出庫存；想改選商品可按「🔄 重新選擇」。' if auto>0 else ''
                    try: await ch.send(f'⏰ **付款提醒**\n您的訂單 #{o["ticket_no"]} 尚未完成付款，若仍要購買，請回到付款訊息按下「💳 完成付款」。'+extra)
                    except discord.HTTPException: pass
            except Exception:
                traceback.print_exc()
    # 逾時自動取消：待付款超過設定時間 → 取消、退庫存、清舊面板、發新面板
    for g in bot.guilds:
        try: auto=int(get(g.id,'auto_cancel_minutes','60') or 0)
        except ValueError: auto=60
        if auto<=0: continue
        cutoff=datetime.now(timezone.utc)-timedelta(minutes=auto)
        for o in q("SELECT * FROM orders WHERE guild_id=? AND status='待付款'",(str(g.id),),True):
            try:
                if datetime.fromisoformat(o['created_at'])>cutoff: continue
                if not await cancel_unpaid_order(o,g): continue
                upd=q('SELECT * FROM orders WHERE id=?',(o['id'],),True)[0]
                await status_announce(upd,g)
                await post_channel_log(g,'order_log_channel_id',f'🕒 **自動取消**｜#{o["ticket_no"]}｜{o["product"]} × {o["quantity"]}｜NT${o["total_price"]:,}｜待付款超過 {auto} 分鐘，庫存已退回。')
                ch=g.get_channel(int(o['channel_id']))
                if isinstance(ch,discord.TextChannel):
                    await _delete_old_panels(ch,o['id'])
                    name=f'工單-{o["ticket_no"]}'
                    if ch.name!=name: await _safe_edit_channel(ch,timeout=10,name=name,reason='待付款逾時自動取消，還原工單名稱')
                    mention=f'<@{o["buyer_id"]}> ' if str(o['buyer_id'] or '').isdigit() else ''
                    try: await ch.send(f'{mention}🕒 **訂單已自動取消**\n訂單 #{o["ticket_no"]} 超過 {auto} 分鐘未付款，已取消並釋出庫存。想購買請使用下方新的購買面板重新下單。')
                    except discord.HTTPException: pass
                    await send_purchase_panel(ch,force=True)
            except Exception:
                traceback.print_exc()

@tasks.loop(seconds=60)
async def payment_reminder_loop():
    # 任何例外（例如資料庫暫時被鎖住）只記錄、不讓迴圈整個停掉；下一分鐘會再跑。
    try: await _payment_tick()
    except Exception:
        print('[PAYMENT_LOOP] 本輪執行失敗，下一輪會重試'); traceback.print_exc()

@payment_reminder_loop.before_loop
async def before_payment_reminder(): await bot.wait_until_ready()

# ── 資料庫自動備份：每 6 小時用 SQLite 官方 backup 備份一份，保留最近 3 份（.bak1 最新）──
def _backup_db_now():
    base=DB_PATH+'.bak'
    tmp=base+'.tmp'
    if os.path.exists(tmp): os.remove(tmp)
    dst=sqlite3.connect(tmp)
    try: db.backup(dst)
    finally: dst.close()
    for n in (3,2):
        if os.path.exists(f'{base}{n-1}'): os.replace(f'{base}{n-1}',f'{base}{n}')
    os.replace(tmp,f'{base}1')

@tasks.loop(hours=6)
async def db_backup_loop():
    try:
        _backup_db_now(); print('[DB_BACKUP] 已備份資料庫')
    except Exception:
        print('[DB_BACKUP] 備份失敗'); traceback.print_exc()

@db_backup_loop.before_loop
async def before_db_backup(): await bot.wait_until_ready()


@bot.event
async def on_error(event_method, *args, **kwargs):
    print(f'Bot event error: {event_method}')
    traceback.print_exc()

def _status_from_name(name):
    n=name or ''
    for st in TICKET_STATUSES:
        if re.match(rf'^{re.escape(st)}(?:[-_ ]|$)', n):
            return st
    # 待倒只屬於代肝的改名狀態；只有完整的「待倒-代肝-工單號」格式才辨識。
    if re.match(r'^待倒[-_ ]+代肝[-_ ]+\d{1,8}(?:[-_ ].*)?$', n, re.I):
        return '待倒'
    return None

def _service_from_name(name):
    n=name or ''
    if '代肝' in n or re.search(r'(?i)(?:^|[-_ ])boost(?:[-_ ]|$)',n): return '代肝'
    if '子彈' in n: return BULLET_SERVICE
    if '幣號' in n or '幣号' in n or re.search(r'(?i)(?:^|[-_ ])coin(?:[-_ ]|$)',n): return '幣號'
    # 舊版幣號名稱沒有服務字樣時，只要有『額度＋數量』格式就視為幣號。
    if re.search(r'\d+(?:\.\d+)?\s*[mM]?(?:\s*[xX×*]\s*\d+|\s*[一二兩三四五六七八九十百]+\s*隻|\s*\d+\s*隻)',n): return '幣號'
    return None

def _parse_legacy_qty(token):
    t=str(token or '').strip().replace('兩','二')
    if t.isdigit():
        return int(t)
    nums={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,'百':100}
    if t in nums: return nums[t]
    if len(t)==2 and t[0]=='十' and t[1] in nums: return 10+nums[t[1]]
    if len(t)==2 and t[1]=='十' and t[0] in nums: return nums[t[0]]*10
    if len(t)==3 and t[1]=='十' and t[0] in nums and t[2] in nums: return nums[t[0]]*10+nums[t[2]]
    return None

def _amount_qty_from_name(name, service):
    n=name or ''
    if service==BULLET_SERVICE:
        mq=re.search(r'(\d+)\s*發',n)
        return BULLET_PRODUCT,(int(mq.group(1)) if mq else 1)
    amount=None; qty=None
    patterns=[
        r'(\d+(?:\.\d+)?)\s*[mM]\s*[xX×*]\s*(\d+)',
        r'(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+)',
        r'(\d+(?:\.\d+)?)\s*[mM]\s*([一二兩三四五六七八九十百]+)\s*隻',
        r'(\d+(?:\.\d+)?)\s*[mM]\s*(\d+)\s*隻',
        r'(\d+(?:\.\d+)?)\s*[mM]\s*(\d+)隻',
    ]
    for pat in patterns:
        ms=list(re.finditer(pat,n,re.I))
        if ms:
            m=ms[-1]; amount=f'{m.group(1)}M'; qty=_parse_legacy_qty(m.group(2)); break
    if amount is None:
        amounts=re.findall(r'(\d+(?:\.\d+)?)\s*[mM]',n)
        if amounts: amount=f'{amounts[-1]}M'
    if service=='幣號' and qty is None:
        ms=list(re.finditer(r'(\d+(?:\.\d+)?)\s*[mM]?\s*([一二兩三四五六七八九十百]+|\d+)\s*隻',n,re.I))
        if ms:
            m=ms[-1]; amount=amount or f'{m.group(1)}M'; qty=_parse_legacy_qty(m.group(2))
    if service=='幣號' and qty is None:
        # 名稱只有額度時，舊工單視為 1 隻；不把額度尾數當成數量。
        qty=1
    elif service!='幣號':
        qty=1
    return amount,qty


@bot.tree.command(name='自動排序工單號', description='依工單開啟時間（越早越小）重新連號為0001、0002、0003…')
@app_commands.describe(包含結單分類='是＝連已放在結單分類的工單也一起搬進工單分類（預設否）')
async def auto_sort_ticket_numbers(i: discord.Interaction, 包含結單分類: bool = False):
    try:
        await i.response.defer(ephemeral=True)
    except discord.NotFound:
        return
    if not admin(i.user):
        return await deny(i)
    result=await renumber_all_tickets(i.guild, 包含結單分類)
    if not result['found']:
        return await i.followup.send('📋 目前沒有辨識到任何工單。',ephemeral=True)
    lines=[f"{a} → {b}" for a,b,_ in result['mapping'] if a!=b][:25]
    n_changed=len([1 for a,b,_ in result['mapping'] if a!=b])
    ok_all = not result['failed'] and not (result.get('arrange') or {}).get('failed')
    e=discord.Embed(title='✅ 工單號自動排序完成' if ok_all else '⚠️ 工單號自動排序完成（部分失敗）',
                    color=UI_COLOR['ok'] if ok_all else UI_COLOR['warn'],timestamp=datetime.now(timezone.utc))
    e.add_field(name='🔎 共偵測',value=f"**{result['found']}** 單",inline=True)
    e.add_field(name='🔢 已重新編號',value=f"**{result['changed']}** 單",inline=True)
    e.add_field(name='❌ 失敗',value=f"**{result['failed']}** 單",inline=True)
    e.add_field(name='🛡️ 服務中（未動）',value=f"**{result.get('in_service',0)}** 單",inline=True)
    e.add_field(name='🧾 狀態卡更新',value=f"**{result.get('refreshed',0)}** 張",inline=True)
    e.add_field(name='🗃️ 結單分類排序',value=f"**{result.get('closed_sorted',0)}** 張",inline=True)
    if result.get('collisions'):
        e.add_field(name='⚠️ 改名失敗',value=clip(f"多半是 Discord 頻道改名限速（同一頻道 10 分鐘只能改 2 次）；失敗的保留舊號、沒有搬分類。\n目前重號：{'、'.join(f'{n:04d}' for n in result['collisions'][:10])}\n請 10 分鐘後再執行一次。"),inline=False)
    e.add_field(name='🗂️ 分類排列',value=clip(_arrange_summary(result.get('arrange'))),inline=False)
    if lines:
        more=f"\n……共 {n_changed} 單有變動" if n_changed>len(lines) else ''
        e.add_field(name='📋 號碼變動（原 → 新）',value=clip('```\n'+'\n'.join(lines)+more+'\n```'),inline=False)
    brand_footer(e,'自動排序工單號')
    try:
        await i.followup.send(embed=e,ephemeral=True)
    except discord.HTTPException:
        # 排序太久、互動已過期（15 分鐘）：改發到目前頻道，避免結果沒人看到。
        try: await i.channel.send(content=i.user.mention,embed=e)
        except Exception as ex: print(f'[RENUMBER] 回報結果失敗｜error={ex!r}')


def _arrange_summary(r):
    if not r or r.get('skipped'):
        extra = ('\n⚠️ ' + '；'.join(r['errors'])) if r and r.get('errors') else ''
        return "分類排列：未執行（找不到幣號工單的分類。請先建立名稱像「幣號工單」的分類，或用 `/設定開單分類` 選「幣號工單」）" + extra
    out = f"分類排列（{r.get('mode','—')}）：搬移 **{r['moved']}** 張｜新建分類 **{r['created']}** 個｜失敗 **{r['failed']}** 張"
    if r.get('chain'):
        out += "\n偵測到的分類：" + "、".join(r['chain'])
    if r.get('inferred'):
        out += f"\n已自動把開單分類設為「{r['inferred']}」"
    if r.get('layout'):
        out += "\n" + "\n".join("• " + x for x in r['layout'])
    if r.get('staging_deleted'):
        out += f"\n已刪除「整理中暫存」分類 {r['staging_deleted']} 個"
    if r.get('staging_kept'):
        out += f"\n⚠️ 有 {r['staging_kept']} 個「整理中暫存」分類內還有工單沒搬出，已保留；再跑一次 `/自動排序工單號` 即可"
    if r.get('errors'):
        out += "\n⚠️ 錯誤：" + "；".join(r['errors'])
    return out

issue_group=app_commands.Group(name='工單號',description='工單號發號管理（查看／設定／同步／本頻道）')

def _peek_next_no(guild):
    r=q('SELECT next_no FROM ticket_counters WHERE guild_id=?',(str(guild.id),),True)
    counter=int(r[0]['next_no']) if r else 1
    live=_live_max_ticket_no(guild)
    return counter, live, max(counter, live+1)

@issue_group.command(name='查看',description='查看目前發號狀況與下一個工單號')
async def issue_view(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    if not admin(i.user): return await deny(i)
    counter,live,nxt=_peek_next_no(i.guild)
    e=discord.Embed(title='🔢 工單號發號狀況',color=UI_COLOR['info'],timestamp=datetime.now(timezone.utc))
    e.add_field(name='計數器',value=f'**{counter:04d}**',inline=True)
    e.add_field(name='現存最大號',value=f'**{live:04d}**',inline=True)
    e.add_field(name='下一張新工單',value=f'**{nxt:04d}**',inline=True)
    brand_footer(e,'/工單號 同步 可把計數器對齊現存最大號')
    await i.followup.send(embed=e,ephemeral=True)

@issue_group.command(name='設定',description='手動設定下一個要發的工單號')
@app_commands.describe(下一號='下一張新工單要使用的號碼，例如 25')
async def issue_set(i: discord.Interaction, 下一號: app_commands.Range[int,1,9999]):
    await i.response.defer(ephemeral=True)
    if not admin(i.user): return await deny(i)
    live=_live_max_ticket_no(i.guild)
    if 下一號<=live:
        return await i.followup.send(f'❌ 現存工單最大號是 **{live:04d}**，下一號必須大於它，否則會撞號。\n想重新連號請用 `/自動排序工單號`。',ephemeral=True)
    q('INSERT INTO ticket_counters(guild_id,next_no) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET next_no=excluded.next_no',(str(i.guild.id),int(下一號)))
    await i.followup.send(f'✅ 已設定：下一張新工單將是 **{int(下一號):04d}**',ephemeral=True)

@issue_group.command(name='同步',description='把計數器同步成「現存工單最大號 + 1」')
async def issue_sync(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    if not admin(i.user): return await deny(i)
    live=_live_max_ticket_no(i.guild)
    q('INSERT INTO ticket_counters(guild_id,next_no) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET next_no=excluded.next_no',(str(i.guild.id),live+1))
    await i.followup.send(f'✅ 已同步：現存最大號 **{live:04d}**，下一張新工單將是 **{live+1:04d}**',ephemeral=True)

@issue_group.command(name='本頻道',description='替目前頻道發一個新工單號並改名（頻道還沒有工單號時使用）')
async def issue_here(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    if not admin(i.user): return await deny(i)
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return await i.followup.send('❌ 請在文字頻道內使用。',ephemeral=True)
    if _resolve_ticket_number(ch):
        return await i.followup.send(f'❌ 這個頻道已經有工單號 **{_resolve_ticket_number(ch)}**，不需要重新發號。\n想整體重排請用 `/自動排序工單號`。',ephemeral=True)
    no=await allocate_ticket_no(i.guild.id,i.guild)
    new_name=f'工單-{no}'
    if not await _safe_edit_channel(ch,timeout=20,name=new_name,reason='CT小舖 手動發號'):
        return await i.followup.send('❌ 改名失敗（可能被 Discord 限速或缺少權限），請稍後再試。',ephemeral=True)
    remember(ch,no)
    await i.followup.send(f'✅ 已發號：**{no}**，頻道改名為 `{new_name}`',ephemeral=True)

bot.tree.add_command(issue_group)


@bot.tree.command(name='設定低庫存提醒',description='免洗帳號剩幾組以下時，在操作紀錄頻道提醒補貨（0＝關閉）')
@app_commands.describe(數量='剩這個數量以下就提醒；預設 3，0＝不提醒')
async def set_low_stock(i:discord.Interaction,數量:app_commands.Range[int,0,1000]):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'low_stock_threshold',int(數量))
    msg='🔕 已關閉低庫存提醒。' if 數量==0 else f'🔔 已設定：免洗帳號任一規格＋登入方式剩 **{數量}** 組以下就提醒（提醒會發在操作紀錄頻道，用完時一定會提醒）。'
    if 數量>0 and not get(i.guild.id,'action_log_channel_id',''): msg+='\n⚠️ 你還沒設定「操作紀錄頻道」，提醒目前不會有地方發，請先設定。'
    await i.response.send_message(msg,ephemeral=True)

def db_snapshot_zip():
    """用 SQLite 官方 backup 取得一致的資料庫快照（不會抓到寫到一半的資料），壓成 zip（手機直接點得開）。"""
    tmp=DB_PATH+'.export.tmp'
    if os.path.exists(tmp): os.remove(tmp)
    dst=sqlite3.connect(tmp)
    try: db.backup(dst)
    finally: dst.close()
    try:
        with open(tmp,'rb') as f: raw=f.read()
    finally:
        try: os.remove(tmp)
        except OSError: pass
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z: z.writestr('trade_bot.db',raw)
    return buf.getvalue()

@bot.tree.command(name='匯出資料庫',description='店長用：下載整個資料庫備份（訂單、餘額、免洗帳號庫存、設定全部）')
async def export_database(i:discord.Interaction):
    if not strict_admin(i.user): return await deny(i, True)
    await i.response.defer(ephemeral=True)
    try: data=db_snapshot_zip()
    except Exception as e:
        traceback.print_exc(); return await i.followup.send(f'❌ 匯出失敗：`{str(e)[:180]}`',ephemeral=True)
    if len(data)>24*1024*1024:
        return await i.followup.send(f'❌ 資料庫壓縮後有 {len(data)/1024/1024:.1f} MB，超過 Discord 上傳上限，請改從 Railway 後台下載。',ephemeral=True)
    fname=f'ct_shop_db_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
    warn='🔐 這份備份**包含全部訂單、會員餘額與免洗帳號（帳號密碼是明文）**，請只存在自己手上，不要轉傳。\n還原方式：用 `/匯入資料庫` 上傳這個 zip（先不填「確認」會先檢查，再輸入「確認覆蓋」才會還原）。'
    dm_ok=False
    try:
        await i.user.send(f'🗄️ **CT小舖資料庫備份**｜{i.guild.name}\n{warn}',file=discord.File(io.BytesIO(data),filename=fname)); dm_ok=True
    except (discord.Forbidden,discord.HTTPException): pass
    tip='📩 已私訊一份給你。' if dm_ok else '⚠️ 私訊失敗（請開啟「允許來自伺服器成員的私人訊息」），請直接用下方這份。'
    await i.followup.send(f'✅ 資料庫備份完成（{len(data)/1024:.0f} KB）。\n{tip}\n{warn}',file=discord.File(io.BytesIO(data),filename=fname),ephemeral=True)
    await post_channel_log(i.guild,'action_log_channel_id',f'🗄️ **匯出資料庫**｜{i.user.mention}')

# ===== 匯入資料庫（還原 /匯出資料庫 的備份）=====
DB_IMPORT_MAX_BYTES = 100 * 1024 * 1024
DB_IMPORT_CONFIRM = '確認覆蓋'
DB_IMPORT_REQUIRED_TABLES = ('settings', 'orders', 'balances', 'accounts', 'products')
DB_IMPORT_COUNT_TABLES = (('orders', '訂單'), ('balances', '會員餘額'), ('accounts', '免洗帳號'), ('order_logs', '訂單操作紀錄'),
                          ('balance_logs', '餘額紀錄'), ('ticket_channels', '工單頻道紀錄'), ('giveaways', '抽獎'))

def db_bytes_from_upload(raw):
    """從上傳的檔案取出資料庫內容：接受 /匯出資料庫 下載的 .zip（內含 trade_bot.db），也接受直接傳 .db。"""
    if raw[:2] == b'PK':
        try: z = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile: raise ValueError('壓縮檔損毀，打不開。')
        with z:
            names = [n for n in z.namelist() if n.lower().endswith('.db') and not n.endswith('/')]
            if not names: raise ValueError('壓縮檔裡找不到 .db 資料庫檔。請使用「/匯出資料庫」下載的檔案。')
            pick = next((n for n in names if n.split('/')[-1] == 'trade_bot.db'), names[0])
            if z.getinfo(pick).file_size > DB_IMPORT_MAX_BYTES: raise ValueError('資料庫檔太大（上限 100 MB）。')
            return z.read(pick)
    return raw

def _open_import_db(path):
    """唯讀＋immutable 開啟還原用的暫存檔：不會產生也不會讀到 -wal／-shm 殘留檔
    （WAL 模式的備份加上上次失敗留下的殘檔，是「file is not a database」的常見原因）。"""
    from urllib.parse import quote
    return sqlite3.connect('file:' + quote(os.path.abspath(path)) + '?mode=ro&immutable=1', uri=True)

def _cleanup_import_files(path):
    for suf in ('', '-wal', '-shm', '-journal'):
        try: os.remove(path + suf)
        except OSError: pass

def inspect_db_file(path, guild_id):
    """檢查要還原的資料庫檔：是 SQLite、結構完整、有必要的資料表。不會動到線上資料庫。
    回傳 {資料表: 筆數, '_guild_settings': 這個伺服器在備份裡有幾筆設定}。"""
    conn = None
    try:
        conn = _open_import_db(path)
        ok = conn.execute('PRAGMA integrity_check').fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as e:
        if conn is not None: conn.close()
        raise ValueError(f'這不是有效的資料庫檔（{str(e)[:80]}）。請使用「/匯出資料庫」下載的檔案。')
    try:
        if ok != 'ok': raise ValueError('資料庫檔案已損毀（完整性檢查沒通過）。')
        missing = [t for t in DB_IMPORT_REQUIRED_TABLES if t not in tables]
        if missing: raise ValueError('這不是 CT小舖 的資料庫（缺少資料表：' + '、'.join(missing) + '）。')
        out = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t, _ in DB_IMPORT_COUNT_TABLES if t in tables}
        out['_guild_settings'] = conn.execute('SELECT COUNT(*) FROM settings WHERE key LIKE ?', (f'g:{guild_id}:%',)).fetchone()[0]
        return out
    except sqlite3.Error as e:
        raise ValueError(f'讀取資料庫失敗（{str(e)[:80]}）。')
    finally:
        conn.close()

def restore_live_db(src_path):
    """把檢查過的資料庫檔整個還原進線上資料庫（沿用同一條連線，不必重開機器人），並重新整理記憶體裡的快取。"""
    db.commit()
    src = _open_import_db(src_path)
    try: src.backup(db)
    finally: src.close()
    db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA foreign_keys=ON')
    init_db()   # 較舊的備份可能缺新的資料表／欄位，補齊
    for _p in SELLABLE_PRODUCTS:
        q('INSERT OR IGNORE INTO products(product,price,stock,enabled,availability_status) VALUES(?,?,?,?,?)', (_p, 0, 0, 1, '正常提供'))
    _SETTINGS_CACHE.clear()
    TICKET_CHANNEL_IDS.clear()
    TICKET_CHANNEL_IDS.update(str(r['channel_id']) for r in q('SELECT channel_id FROM ticket_channels', (), True))

def _db_counts_text(info, cur):
    return '\n'.join(f'• {label}：目前 {cur[t]} → 備份 {info[t]}' for t, label in DB_IMPORT_COUNT_TABLES if t in info and t in cur)

@bot.tree.command(name='匯入資料庫', description='店長用：用「/匯出資料庫」的備份檔還原整個資料庫（會蓋掉目前全部資料）')
@app_commands.describe(檔案='/匯出資料庫 下載的 .zip（或裡面的 trade_bot.db）', 確認='第一次先不填：只檢查檔案、顯示還原後的樣子。確定要還原時，輸入「確認覆蓋」')
async def import_database(i: discord.Interaction, 檔案: discord.Attachment, 確認: str = ''):
    if not strict_admin(i.user): return await deny(i, True)
    await i.response.defer(ephemeral=True)
    if 檔案.size > DB_IMPORT_MAX_BYTES:
        return await i.followup.send('❌ 檔案太大（上限 100 MB）。', ephemeral=True)
    tmp = DB_PATH + '.import.tmp'
    try:
        try: data = db_bytes_from_upload(await 檔案.read())
        except (ValueError, discord.HTTPException) as e:
            return await i.followup.send(f'❌ {e}', ephemeral=True)
        _cleanup_import_files(tmp)      # 清掉上次失敗留下的暫存檔與 -wal／-shm
        if data[:16] != b'SQLite format 3\x00':
            return await i.followup.send('❌ 這個檔案不是 SQLite 資料庫（檔頭不對）。請上傳「/匯出資料庫」下載的 .zip，或裡面的 trade_bot.db；不要上傳「設定檔 .txt」或截圖。', ephemeral=True)
        with open(tmp, 'wb') as f: f.write(data)
        try: info = inspect_db_file(tmp, i.guild.id)
        except ValueError as e:
            return await i.followup.send(f'❌ {e}', ephemeral=True)
        cur = {t: q(f'SELECT COUNT(*) AS n FROM {t}', (), True)[0]['n'] for t, _ in DB_IMPORT_COUNT_TABLES}
        warn = ''
        if info['_guild_settings'] == 0:
            warn = '\n⚠️ 這份備份裡**沒有這個伺服器的設定**，可能是別的伺服器的備份。還原後各種頻道、身分組的設定會對不上，需要重新設定。'
        if str(確認).strip() != DB_IMPORT_CONFIRM:
            return await i.followup.send(
                '🔍 **檔案檢查通過，還沒有還原。**\n' + _db_counts_text(info, cur) + warn +
                f'\n\n確定要還原，請用同一個檔案再執行一次 `/匯入資料庫`，「確認」欄輸入 `{DB_IMPORT_CONFIRM}`。'
                '\n還原會蓋掉目前全部資料；還原前機器人會先把目前的資料庫備份一份，一起傳給你。', ephemeral=True)
        async with lock:
            before = db_snapshot_zip()   # 還原前先把現在的資料庫留一份，萬一還原錯了還能救回來
            try: restore_live_db(tmp)
            except Exception as e:
                traceback.print_exc()
                return await i.followup.send(f'❌ 還原失敗：`{str(e)[:180]}`\n目前資料應該沒有被改動，建議先用 `/匯出資料庫` 備份一份再確認。', ephemeral=True)
        after = {t: q(f'SELECT COUNT(*) AS n FROM {t}', (), True)[0]['n'] for t, _ in DB_IMPORT_COUNT_TABLES}
        try: await post_channel_log(i.guild, 'action_log_channel_id', f'♻️ **匯入資料庫**｜{i.user.mention}｜訂單 {cur["orders"]} → {after["orders"]}')
        except Exception: pass
        msg = ('✅ **資料庫已還原。**不用重開機器人。\n' + _db_counts_text(info, cur) + warn +
               '\n\n⚠️ 還原前的資料庫備份在下面；確認沒問題之後請自己妥善保管，檔案含帳號明文。')
        if len(before) <= 20 * 1024 * 1024:
            fname = f'ct_shop_db_before_import_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
            await i.followup.send(msg, file=discord.File(io.BytesIO(before), filename=fname), ephemeral=True)
        else:
            await i.followup.send(msg + f'\n（還原前的備份有 {len(before)/1024/1024:.0f} MB，太大傳不出來，請直接從 Railway 後台下載。）', ephemeral=True)
    finally:
        _cleanup_import_files(tmp)

@bot.tree.command(name='匯出設定', description='匯出全部設定：文字訊息、價格、代肝價格、餘額、工單號（可選含免洗帳號庫存）')
@app_commands.describe(包含帳號庫存='是否連同「可發」的免洗帳號一起匯出（帳號密碼會是明文，請妥善保管；預設不含）')
async def export_settings(i, 包含帳號庫存:bool=False):
    if not strict_admin(i.user): return await deny(i, True)
    await i.response.defer(ephemeral=True)
    data=config_bytes(i.guild.id, bool(包含帳號庫存))
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    # 檔名用英數、副檔名用 .txt：手機版 Discord 對中文檔名與 .json 常常點了沒反應。
    fname=f'ct_shop_settings_{stamp}.txt'
    warn='⚠️ 設定檔可能包含付款資訊、頻道 ID、身分組 ID 等資料，請勿公開分享。\n\n這份檔案可直接交給 CT小舖 Bot 使用「/匯入設定」。'
    if 包含帳號庫存: warn+='\n🔐 這份檔案**包含免洗帳號（帳號密碼是明文）**，請只存在自己手上，不要轉傳。'
    dm_ok=False
    try:
        await i.user.send(f'📦 **CT小舖設定檔**｜{i.guild.name}\n{warn}',file=discord.File(io.BytesIO(data),filename=fname))
        dm_ok=True
    except (discord.Forbidden,discord.HTTPException):
        pass
    tip='📩 已私訊一份給你，到私訊裡長按檔案即可存到「檔案」。' if dm_ok else '⚠️ 私訊失敗（請到「隱私設定」開啟「允許來自伺服器成員的私人訊息」後再試一次）。'
    await i.followup.send(f'✅ 已匯出目前設定。\n{tip}\n{warn}',file=discord.File(io.BytesIO(data),filename=fname),ephemeral=True)


def decode_config_bytes(raw:bytes):
    """容錯解析設定檔：處理 BOM、UTF-16、Big5/GBK 另存、重複 BOM、前後雜訊。"""
    last=None
    cands=[]
    if raw.startswith((b'\xff\xfe',b'\xfe\xff')): cands.append('utf-16')
    cands+=['utf-8-sig','utf-8','utf-16','cp950','gb18030']
    for enc in cands:
        try: text=raw.decode(enc)
        except UnicodeDecodeError as e: last=e; continue
        text=text.replace('\ufeff','').strip()
        a,b=text.find('{'),text.rfind('}')
        if a<0 or b<a: continue
        try: return json.loads(text[a:b+1])
        except json.JSONDecodeError as e: last=e
    if last: raise last
    raise ValueError('檔案內容不是有效的 JSON')

@bot.tree.command(name='匯入設定', description='從 CT小舖 設定文字檔恢復設定')
@app_commands.describe(檔案='使用 /匯出設定 產生的 .json 設定檔（舊版 .txt JSON 也可）',覆蓋庫存與餘額='預設否：已存在的商品庫存、會員餘額維持現狀，只補上檔案裡有、目前沒有的。選是＝全部以檔案為準（會蓋掉匯出後的新變動）')
async def import_settings(i, 檔案:discord.Attachment, 覆蓋庫存與餘額:bool=False):
    if not strict_admin(i.user): return await deny(i, True)
    await i.response.defer(ephemeral=True)
    if 檔案.size>2*1024*1024: return await i.followup.send('❌ 設定檔過大（上限 2 MB），請使用「/匯出設定」產生的檔案。',ephemeral=True)
    try:
        raw=await 檔案.read(); data=decode_config_bytes(raw)
    except (UnicodeDecodeError,json.JSONDecodeError,discord.HTTPException,ValueError) as e:
        return await i.followup.send(f'❌ 無法讀取設定檔：{e}\n請直接使用「/匯出設定」產生的原始檔案，不要用記事本／備忘錄／其他 App 開啟後另存或複製貼上。',ephemeral=True)
    if not isinstance(data,dict) or data.get('format')!='CT小舖設定檔': return await i.followup.send('❌ 這不是 CT小舖 的設定檔。請使用「/匯出設定」產生的檔案。',ephemeral=True)
    try: version=int(data.get('version',0))
    except (TypeError,ValueError): version=0
    if version not in (1,CONFIG_EXPORT_VERSION): return await i.followup.send('❌ 設定檔版本不相容，目前支援 V1 舊檔與 V2 新格式。請重新匯出設定。',ephemeral=True)
    settings=data.get('settings',{}); products=data.get('products',[]); boost_prices_data=data.get('boost_prices',[]); balances_data=data.get('balances',[])
    accounts_data=data.get('accounts',[]); ticket_counter=data.get('ticket_counter',0)
    if not isinstance(accounts_data,list): return await i.followup.send('❌ 設定檔格式不完整（帳號資料）。',ephemeral=True)
    try: ticket_counter=int(ticket_counter or 0)
    except (TypeError,ValueError): ticket_counter=0
    if version==1: products=[dict(x) for x in products if isinstance(x,dict)]
    if not isinstance(settings,dict) or not isinstance(products,list) or not isinstance(boost_prices_data,list) or not isinstance(balances_data,list): return await i.followup.send('❌ 設定檔格式不完整。',ephemeral=True)
    try:
        for item in products:
            if not isinstance(item,dict) or not isinstance(item.get('product'),str): raise ValueError('商品資料格式錯誤')
            if float(item.get('price',0) or 0)<0 or int(item.get('stock',0))<0: raise ValueError('商品價格／庫存不能小於 0')
            if str(item.get('availability_status','正常提供')) not in AVAILABILITY: raise ValueError('商品供應狀態錯誤')
        for item in boost_prices_data:
            if not isinstance(item,dict) or str(item.get('tier')) not in BOOST_TIERS: raise ValueError('代肝額度格式錯誤')
            if int(item.get('price',0))<0: raise ValueError('代肝價格不能小於 0')
        for k in settings:
            if not isinstance(k,str) or not k or len(k)>100: raise ValueError('設定名稱格式錯誤')
        for item in accounts_data:
            if not isinstance(item,dict) or not str(item.get('content','')).strip() or item.get('product') not in COIN_PRODUCTS or item.get('login_type') not in POOL_TYPES:
                raise ValueError('帳號庫存資料格式錯誤')
        for item in balances_data:
            if not isinstance(item,dict) or not str(item.get('user_id','')).isdigit(): raise ValueError('會員餘額資料格式錯誤')
            if int(item.get('balance',0)) < 0: raise ValueError('會員餘額不能小於 0')
    except (TypeError,ValueError) as e:
        return await i.followup.send(f'❌ 設定檔驗證失敗：{e}',ephemeral=True)
    try:
        with db:
            cur=db.cursor(); prefix=f'g:{i.guild.id}:'
            for k,v in settings.items():
                if not isinstance(k,str) or not k or len(k)>100: continue
                cur.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(prefix+k,str(v)))
            for item in products:
                if not isinstance(item,dict) or not item.get('product'): continue
                product=str(item['product'])[:100]; price_v=float(item.get('price',0) or 0); price_v=int(price_v) if price_v==int(price_v) else round(price_v,4); stock_v=int(item.get('stock',0) or 0); enabled_v=1 if int(item.get('enabled',1) or 0) else 0; availability_v=str(item.get('availability_status','正常提供'))
                if price_v<0 or stock_v<0 or availability_v not in AVAILABILITY: continue
                if 覆蓋庫存與餘額:
                    cur.execute('''INSERT INTO products(product,price,stock,enabled,availability_status) VALUES(?,?,?,?,?) ON CONFLICT(product) DO UPDATE SET price=excluded.price,stock=excluded.stock,enabled=excluded.enabled,availability_status=excluded.availability_status''',(product,price_v,stock_v,enabled_v,availability_v))
                else:
                    # 不動現有庫存（現有庫存裡有進行中訂單的預扣，直接覆蓋會讓之後的退回對不起來）。
                    cur.execute('''INSERT INTO products(product,price,stock,enabled,availability_status) VALUES(?,?,?,?,?) ON CONFLICT(product) DO UPDATE SET price=excluded.price,enabled=excluded.enabled,availability_status=excluded.availability_status''',(product,price_v,stock_v,enabled_v,availability_v))
            for item in boost_prices_data:
                if not isinstance(item,dict) or not item.get('tier'): continue
                tier=str(item['tier']); price_v=int(item.get('price',0) or 0)
                if tier not in BOOST_TIERS or price_v<0: continue
                cur.execute('''INSERT INTO boost_prices(guild_id,tier,price) VALUES(?,?,?) ON CONFLICT(guild_id,tier) DO UPDATE SET price=excluded.price''',(str(i.guild.id),tier,price_v))
            for item in balances_data:
                if not isinstance(item,dict) or not str(item.get('user_id','')).isdigit(): continue
                user_id=str(item['user_id']); balance_v=int(item.get('balance',0) or 0)
                if balance_v < 0: continue
                if 覆蓋庫存與餘額:
                    cur.execute('SELECT balance FROM balances WHERE user_id=?',(user_id,)); _old=cur.fetchone()
                    cur.execute('INSERT INTO balances(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance',(user_id,balance_v))
                    if _old is not None and int(_old['balance'])!=balance_v:
                        cur.execute('INSERT INTO balance_logs(guild_id,user_id,operator_id,amount,balance_after,action,created_at,note) VALUES(?,?,?,?,?,?,?,?)',(str(i.guild.id),user_id,str(i.user.id),balance_v-int(_old['balance']),balance_v,'匯入覆蓋',now(),f'匯入設定覆蓋：{int(_old["balance"])} → {balance_v}'))
                else:
                    cur.execute('INSERT OR IGNORE INTO balances(user_id,balance) VALUES(?,?)',(user_id,balance_v))
            acc_added=0
            for item in accounts_data:
                c=str(item['content']).strip()
                cur.execute('SELECT 1 FROM accounts WHERE guild_id=? AND content=? LIMIT 1',(str(i.guild.id),c))
                if cur.fetchone(): continue
                cur.execute('INSERT INTO accounts(guild_id,product,content,login_type,created_at) VALUES(?,?,?,?,?)',(str(i.guild.id),item['product'],c,item['login_type'],now())); acc_added+=1
            if ticket_counter>0:
                cur.execute('INSERT INTO ticket_counters(guild_id,next_no) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET next_no=MAX(next_no,excluded.next_no)',(str(i.guild.id),ticket_counter))
            db.commit()
    except (ValueError,sqlite3.Error) as e: return await i.followup.send(f'❌ 匯入失敗，沒有完成設定更新：{e}',ephemeral=True)
    _SETTINGS_CACHE.clear()   # 匯入直接寫資料庫，快取一律作廢
    source_gid=str(data.get('source_guild_id',''))
    warnings=[]
    if source_gid and source_gid!=str(i.guild.id):
        id_keys=[k for k in settings if k.endswith('_id') or k.endswith('_ids')]
        if id_keys: warnings.append('⚠️ 來源伺服器與目前伺服器不同；頻道／身分組 ID 類設定可能需要重新設定。')
    await i.followup.send(f'✅ 設定匯入完成。\n已恢復：{len(settings)} 項文字設定、{len(products)} 個商品設定、{len(boost_prices_data)} 個代肝價格、{len(balances_data)} 筆會員餘額'+(f'、{acc_added} 組免洗帳號' if accounts_data else '')+(f'、下一個工單號 {ticket_counter}' if ticket_counter>0 else '')+'。\n' + ('\n'.join(warnings)+'\n' if warnings else '') + ('\n⚠️ 已依檔案覆蓋庫存與會員餘額（餘額變動已寫入流水）。' if 覆蓋庫存與餘額 else '\nℹ️ 已存在的商品庫存與會員餘額維持現狀，只補上新的；要以檔案為準請加上「覆蓋庫存與餘額：是」。')+'\n⚠️ 訂單與交易紀錄不會被匯入。',ephemeral=True)

# ════════════════ 抽獎 ════════════════
# 流程：/建立抽獎 → 客人按「參加抽獎」→ 店長 /開獎（手動）。獎品是文字，由店長自己發。
GIVEAWAY_JOIN_ID='ct_giveaway:join'

def giveaway_get(gid):
    rows=q('SELECT * FROM giveaways WHERE id=?',(int(gid),),True)
    return rows[0] if rows else None

def giveaway_count(gid):
    return q('SELECT COUNT(*) AS n FROM giveaway_entries WHERE giveaway_id=?',(int(gid),),True)[0]['n']

def giveaway_pick(entry_ids, count, exclude=()):
    """從參加者中隨機抽出 count 位（不重複、排除 exclude）。用系統亂數，結果無法預測。"""
    pool=[u for u in dict.fromkeys(str(x) for x in entry_ids) if u not in set(str(x) for x in exclude)]
    count=max(0,min(int(count),len(pool)))
    return random.SystemRandom().sample(pool,count)

async def _giveaway_still_in_guild(guild, uid):
    """還在伺服器內嗎？機器人沒開「成員」權限，快取不可靠，所以快取找不到時要問 Discord。
    只有確定「查無此人」才算已離開；網路錯誤等暫時性問題一律當作還在，避免誤殺得獎者。"""
    try: uid=int(uid)
    except (TypeError, ValueError): return False
    if guild.get_member(uid): return True
    try:
        await guild.fetch_member(uid); return True
    except discord.NotFound: return False
    except discord.HTTPException: return True

async def giveaway_pick_valid(guild, entry_ids, count, exclude=()):
    """隨機抽 count 位，而且是現在還在伺服器內的人。回傳 (得獎者, 抽的過程中發現已離開的人數)。"""
    ex=set(str(x) for x in exclude)
    pool=[u for u in dict.fromkeys(str(x) for x in entry_ids) if u not in ex]
    random.SystemRandom().shuffle(pool)
    winners=[]; gone=0
    for u in pool:
        if len(winners)>=int(count): break
        if await _giveaway_still_in_guild(guild,u): winners.append(u)
        else: gone+=1
    return winners, gone

def giveaway_embed(g, ended=False, winner_ids=None, cancelled=False):
    n=giveaway_count(g['id'])
    if cancelled: title,color='🚫 抽獎已取消',UI_COLOR['danger']
    elif ended: title,color='🎉 抽獎結束',UI_COLOR['ok']
    else: title,color='🎁 抽獎活動',UI_COLOR['brand']
    lines=[f"**獎品：{g['prize']}**"]
    if g['note']: lines.append(str(g['note']))
    lines.append(f"\n🏆 名額：**{g['winners']}** 位")
    lines.append(f"👥 目前參加：**{n}** 人")
    if ended and not cancelled:
        ids=winner_ids if winner_ids is not None else [x for x in str(g['winner_ids']).split(',') if x]
        lines.append('\n**得獎者：** '+('、'.join(f'<@{u}>' for u in ids) if ids else '（沒有人參加）'))
    elif not ended:
        lines.append('\n按下方按鈕參加，店長開獎後會公布得獎者。')
    e=discord.Embed(title=title,description='\n'.join(lines),color=color)
    e.set_footer(text=f"抽獎編號 #{g['id']}")
    return e

class GiveawayView(discord.ui.View):
    def __init__(self, disabled=False):
        super().__init__(timeout=None)
        b=discord.ui.Button(label='🎁 參加抽獎',style=discord.ButtonStyle.success,custom_id=GIVEAWAY_JOIN_ID,disabled=disabled)
        b.callback=giveaway_join_click; self.add_item(b)

class GiveawayLeaveView(discord.ui.View):
    def __init__(self, gid, uid):
        super().__init__(timeout=60); self.gid=int(gid); self.uid=str(uid)
        b=discord.ui.Button(label='退出抽獎',style=discord.ButtonStyle.danger)
        b.callback=self.leave; self.add_item(b)
    async def leave(self, i:discord.Interaction):
        g=giveaway_get(self.gid)
        if not g or g['status']!='open':
            return await i.response.edit_message(content='❌ 這個抽獎已經結束，不能退出了。',view=None)
        q('DELETE FROM giveaway_entries WHERE giveaway_id=? AND user_id=?',(self.gid,self.uid))
        await i.response.edit_message(content='✅ 已退出抽獎。想再參加可以重新按「參加抽獎」。',view=None)
        giveaway_schedule_refresh(i.guild,self.gid)

async def giveaway_join_click(i:discord.Interaction):
    rows=q('SELECT * FROM giveaways WHERE message_id=? AND guild_id=?',(str(i.message.id) if i.message else '',str(i.guild.id) if i.guild else ''),True)
    if not rows:
        return await i.response.send_message('❌ 找不到這個抽獎（可能已被刪除）。',ephemeral=True)
    g=rows[0]
    if g['status']!='open':
        return await i.response.send_message('❌ 這個抽獎已經結束了。',ephemeral=True)
    if i.user.bot:
        return await i.response.send_message('❌ 機器人不能參加抽獎。',ephemeral=True)
    uid=str(i.user.id)
    # 主鍵 (抽獎編號, 使用者) 保證每個人只會有一筆；兩次快速點擊也不會重複。
    cur=db.cursor()
    cur.execute('INSERT OR IGNORE INTO giveaway_entries(giveaway_id,user_id,joined_at) VALUES(?,?,?)',(g['id'],uid,now()))
    db.commit()
    if cur.rowcount==0:
        return await i.response.send_message(f'你已經參加了（目前共 **{giveaway_count(g["id"])}** 人）。要退出請按下面的按鈕。',view=GiveawayLeaveView(g['id'],uid),ephemeral=True)
    await i.response.send_message(f'✅ 參加成功！目前共 **{giveaway_count(g["id"])}** 人參加。',ephemeral=True)
    giveaway_schedule_refresh(i.guild,g['id'])

async def _giveaway_refresh_message(guild, g, **kw):
    """更新抽獎訊息；訊息被刪或沒權限就略過，不影響開獎結果。"""
    try:
        ch=guild.get_channel(int(g['channel_id'])) or await guild.fetch_channel(int(g['channel_id']))
        msg=await ch.fetch_message(int(g['message_id']))
        await msg.edit(embed=giveaway_embed(g,**kw),view=GiveawayView(disabled=kw.get('ended',False) or kw.get('cancelled',False)))
        return ch
    except Exception as ex:
        print(f'[GIVEAWAY] 更新抽獎訊息失敗｜id={g["id"]}｜error={ex!r}')
        return None

GIVEAWAY_REFRESH_DELAY=3          # 秒：很多人同時按時合併成一次更新，避免撞到 Discord 編輯訊息的速率限制
_GIVEAWAY_REFRESH_PENDING={}

def giveaway_schedule_refresh(guild, gid):
    """有人參加或退出後，稍後把訊息上的「目前參加 N 人」更新成最新數字（合併多次更新）。"""
    gid=int(gid)
    if gid in _GIVEAWAY_REFRESH_PENDING: return
    async def _run():
        try:
            await asyncio.sleep(GIVEAWAY_REFRESH_DELAY)
            g=giveaway_get(gid)
            if g and g['status']=='open': await _giveaway_refresh_message(guild,g)
        except Exception as ex:
            print(f'[GIVEAWAY] 更新人數失敗｜id={gid}｜error={ex!r}')
        finally:
            _GIVEAWAY_REFRESH_PENDING.pop(gid,None)
    _GIVEAWAY_REFRESH_PENDING[gid]=asyncio.ensure_future(_run())

@bot.tree.command(name='建立抽獎',description='在頻道發一則抽獎，客人按按鈕參加，之後由你手動開獎')
@app_commands.describe(獎品='獎品文字，例如「100M 幣號 × 1」',名額='要抽出幾位得獎者（預設 1）',說明='（選填）補充說明或參加條件',頻道='（選填）要發在哪個頻道；不選就發在目前頻道')
async def create_giveaway(i:discord.Interaction,獎品:str,名額:app_commands.Range[int,1,50]=1,說明:str='',頻道:discord.TextChannel|None=None):
    if not admin(i.user): return await deny(i)
    prize=str(獎品).strip(); note=str(說明).strip()
    if not prize: return await i.response.send_message('❌ 請填獎品。',ephemeral=True)
    if len(prize)>200: return await i.response.send_message('❌ 獎品文字最多 200 字。',ephemeral=True)
    if len(note)>800: return await i.response.send_message('❌ 說明最多 800 字。',ephemeral=True)
    target=頻道 or i.channel
    if not isinstance(target,discord.TextChannel):
        return await i.response.send_message('❌ 請在文字頻道使用，或用「頻道」選一個文字頻道。',ephemeral=True)
    await i.response.defer(ephemeral=True)
    cur=db.cursor()
    cur.execute('INSERT INTO giveaways(guild_id,channel_id,prize,note,winners,host_id,created_at) VALUES(?,?,?,?,?,?,?)',(str(i.guild.id),str(target.id),prize,note,int(名額),str(i.user.id),now()))
    db.commit(); gid=cur.lastrowid
    g=giveaway_get(gid)
    try:
        msg=await target.send(embed=giveaway_embed(g),view=GiveawayView())
    except discord.HTTPException as e:
        q('DELETE FROM giveaways WHERE id=?',(gid,))
        return await i.followup.send(f'❌ 發不出去：`{str(e)[:150]}`。請確認機器人在 {target.mention} 有「發送訊息」「嵌入連結」權限。',ephemeral=True)
    q('UPDATE giveaways SET message_id=? WHERE id=?',(str(msg.id),gid))
    try: await post_channel_log(i.guild,'action_log_channel_id',f'🎁 **建立抽獎 #{gid}**｜{target.mention}｜{prize}｜{int(名額)} 位｜{i.user.mention}')
    except Exception: pass
    await i.followup.send(f'✅ 已建立抽獎 **#{gid}**（{target.mention}）。開獎請用 `/開獎` 並輸入編號 {gid}。',ephemeral=True)

async def _giveaway_ac(i:discord.Interaction,current:str):
    rows=q("SELECT id,prize,status FROM giveaways WHERE guild_id=? ORDER BY id DESC LIMIT 25",(str(i.guild.id),),True) if i.guild else []
    out=[]
    for r in rows:
        label=f"#{r['id']}｜{'進行中' if r['status']=='open' else '已結束' if r['status']=='ended' else '已取消'}｜{r['prize']}"[:100]
        if not current or current.lstrip('#') in str(r['id']) or current in label: out.append(app_commands.Choice(name=label,value=int(r['id'])))
    return out[:25]

@bot.tree.command(name='開獎',description='抽出得獎者並公布（只能開一次；要補抽請用 /重抽）')
@app_commands.describe(編號='抽獎編號（輸入時會跳出清單）')
@app_commands.autocomplete(編號=_giveaway_ac)
async def draw_giveaway(i:discord.Interaction,編號:int):
    if not admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    async with lock:
        g=giveaway_get(編號)
        if not g or g['guild_id']!=str(i.guild.id): return await i.followup.send('❌ 找不到這個抽獎編號。',ephemeral=True)
        if g['status']!='open': return await i.followup.send(f'❌ 抽獎 #{g["id"]} 已經{"開過獎" if g["status"]=="ended" else "取消"}了。要補抽請用 `/重抽`。',ephemeral=True)
        entries=[r['user_id'] for r in q('SELECT user_id FROM giveaway_entries WHERE giveaway_id=? ORDER BY joined_at',(g['id'],),True)]
        # 已經離開伺服器的人不算：抽到才查，查不到就換下一位。
        winners,gone=await giveaway_pick_valid(i.guild,entries,g['winners'])
        q("UPDATE giveaways SET status='ended',winner_ids=?,ended_at=? WHERE id=? AND status='open'",(','.join(winners),now(),g['id']))
    g=giveaway_get(g['id'])
    ch=await _giveaway_refresh_message(i.guild,g,ended=True,winner_ids=winners)
    mentions='、'.join(f'<@{u}>' for u in winners)
    if ch:
        try:
            if winners: await ch.send(f'🎉 恭喜 {mentions} 獲得 **{g["prize"]}**！請聯絡店長領獎。（抽獎 #{g["id"]}）',allowed_mentions=discord.AllowedMentions(users=True))
            else: await ch.send(f'抽獎 #{g["id"]}（{g["prize"]}）結束，沒有人參加。')
        except discord.HTTPException as e: print(f'[GIVEAWAY] 公布失敗｜{e!r}')
    try: await post_channel_log(i.guild,'action_log_channel_id',f'🎉 **開獎 #{g["id"]}**｜{g["prize"]}｜得獎：{mentions or "無"}｜參加 {len(entries)} 人｜{i.user.mention}')
    except Exception: pass
    note='' if ch else '\n⚠️ 找不到原本的抽獎訊息，沒辦法公布到頻道，請自己通知得獎者。'
    await i.followup.send(f'✅ 已開獎 #{g["id"]}（參加 {len(entries)} 人）。\n得獎者：{mentions or "無"}'+(f'\n（抽的過程中有 {gone} 位已離開伺服器，已略過）' if gone else '')+note,ephemeral=True)

@bot.tree.command(name='重抽',description='開獎後有人棄權時，再補抽新的得獎者（不會抽到已得獎的人）')
@app_commands.describe(編號='抽獎編號',人數='要補抽幾位（預設 1）')
@app_commands.autocomplete(編號=_giveaway_ac)
async def reroll_giveaway(i:discord.Interaction,編號:int,人數:app_commands.Range[int,1,50]=1):
    if not admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    async with lock:
        g=giveaway_get(編號)
        if not g or g['guild_id']!=str(i.guild.id): return await i.followup.send('❌ 找不到這個抽獎編號。',ephemeral=True)
        if g['status']!='ended': return await i.followup.send('❌ 這個抽獎還沒開獎（或已取消），請先用 `/開獎`。',ephemeral=True)
        old=[x for x in str(g['winner_ids']).split(',') if x]
        entries=[r['user_id'] for r in q('SELECT user_id FROM giveaway_entries WHERE giveaway_id=?',(g['id'],),True)]
        new,_gone=await giveaway_pick_valid(i.guild,entries,人數,exclude=old)
        if not new: return await i.followup.send('❌ 沒有其他人可以補抽了（所有參加者都已得獎或已離開）。',ephemeral=True)
        q('UPDATE giveaways SET winner_ids=? WHERE id=?',(','.join(old+new),g['id']))
    g=giveaway_get(g['id'])
    ch=await _giveaway_refresh_message(i.guild,g,ended=True)
    mentions='、'.join(f'<@{u}>' for u in new)
    if ch:
        try: await ch.send(f'🔁 補抽結果：恭喜 {mentions} 獲得 **{g["prize"]}**！請聯絡店長領獎。（抽獎 #{g["id"]}）',allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException as e: print(f'[GIVEAWAY] 公布失敗｜{e!r}')
    try: await post_channel_log(i.guild,'action_log_channel_id',f'🔁 **重抽 #{g["id"]}**｜新得獎：{mentions}｜{i.user.mention}')
    except Exception: pass
    await i.followup.send(f'✅ 已補抽：{mentions}'+('' if ch else '\n⚠️ 找不到原本的抽獎訊息，請自己通知得獎者。'),ephemeral=True)

@bot.tree.command(name='取消抽獎',description='取消一個還沒開獎的抽獎')
@app_commands.describe(編號='抽獎編號')
@app_commands.autocomplete(編號=_giveaway_ac)
async def cancel_giveaway(i:discord.Interaction,編號:int):
    if not admin(i.user): return await deny(i)
    await i.response.defer(ephemeral=True)
    async with lock:
        g=giveaway_get(編號)
        if not g or g['guild_id']!=str(i.guild.id): return await i.followup.send('❌ 找不到這個抽獎編號。',ephemeral=True)
        if g['status']!='open': return await i.followup.send('❌ 這個抽獎已經結束或取消了。',ephemeral=True)
        q("UPDATE giveaways SET status='cancelled',ended_at=? WHERE id=? AND status='open'",(now(),g['id']))
    g=giveaway_get(g['id'])
    await _giveaway_refresh_message(i.guild,g,ended=True,cancelled=True)
    try: await post_channel_log(i.guild,'action_log_channel_id',f'🚫 **取消抽獎 #{g["id"]}**｜{g["prize"]}｜{i.user.mention}')
    except Exception: pass
    await i.followup.send(f'✅ 已取消抽獎 #{g["id"]}。',ephemeral=True)

@bot.tree.command(name='抽獎列表',description='看目前進行中的抽獎與參加人數')
async def list_giveaways(i:discord.Interaction):
    if not admin(i.user): return await deny(i)
    rows=q("SELECT * FROM giveaways WHERE guild_id=? AND status='open' ORDER BY id DESC LIMIT 20",(str(i.guild.id),),True)
    if not rows: return await i.response.send_message('目前沒有進行中的抽獎。用 `/建立抽獎` 建一個。',ephemeral=True)
    lines=[f"**#{r['id']}**｜{r['prize']}｜{r['winners']} 位｜參加 {giveaway_count(r['id'])} 人｜<#{r['channel_id']}>" for r in rows]
    await i.response.send_message('🎁 **進行中的抽獎**\n'+'\n'.join(lines),ephemeral=True)


# ══════════════════════ 評價系統 ══════════════════════
# /發送評價面板 商品:xxx → 在頻道發一則大家看得到的面板；客人按 1～5 顆星的按鈕 → 跳出輸入框（可留空）→
# 評價用小卡片公開貼到評價頻道（感謝／商品／星級／評價）。面板外觀：/設定評價面板；貼到哪：/設定評價頻道。
REVIEW_BUTTON_PREFIX = 'ct_review:star:'     # 後面接 1～5
REVIEW_COOLDOWN_SEC = 3600                   # 同一個人對「同一張面板」1 小時內只能評價一次，避免洗版
REVIEW_NO_COMMENT = '無評價'
REVIEW_PANEL_DEFAULT = {
    'title': '⭐ 服務評價',
    'description': '感謝您的惠顧！請按下方星星給我們評分 👇\n接著可以留下想說的話，不想寫也可以直接送出。',
    'color': 'F1C40F',
    'image': '',
}

def parse_review_color(text, default=0xF1C40F):
    t = str(text or '').strip().lstrip('#').lower()
    if re.fullmatch(r'[0-9a-f]{6}', t): return int(t, 16)
    return default

def review_stars(n):
    return '⭐' * max(0, min(5, int(n)))

def load_review_panel(guild_id):
    d = dict(REVIEW_PANEL_DEFAULT)
    try:
        saved = json.loads(get(guild_id, 'review_panel', '') or '{}')
        if isinstance(saved, dict): d.update({k: str(v) for k, v in saved.items() if k in d})
    except (ValueError, TypeError): pass
    return d

def build_review_panel_embed(guild_id, product):
    c = load_review_panel(guild_id)
    e = discord.Embed(title=c['title'][:256], description=c['description'][:3000], color=parse_review_color(c['color']))
    e.add_field(name='商品', value=str(product)[:1000], inline=False)
    if c['image'].startswith(('http://', 'https://')): e.set_image(url=c['image'])
    return brand_footer(e, '按下方星星開始評價')

def panel_product_from_message(msg):
    """商品名稱存在面板訊息的「商品」欄位裡（重啟機器人後也讀得到，不需要額外存檔）。"""
    try:
        for emb in msg.embeds:
            for f in emb.fields:
                if f.name == '商品': return str(f.value)
    except Exception: pass
    return ''

def review_card_text(user_id, product, rating, comment):
    c = str(comment or '').strip() or REVIEW_NO_COMMENT
    return (f'◆ **感謝：**<@{user_id}> 光顧本店\n◆ **商品：**{product}\n◆ **星級：**{review_stars(rating)}\n◆ **評價：**{c}')

def build_review_card(user, product, rating, comment):
    """小卡片：標題「來自 xxx 的評論」、右上角放客人的頭貼。"""
    color = UI_COLOR['ok'] if rating >= 4 else UI_COLOR['warn'] if rating == 3 else UI_COLOR['danger']
    e = discord.Embed(title=f'🌟 來自 {user.display_name} 的評論'[:256], description=review_card_text(user.id, product, rating, comment)[:4000],
                      color=color, timestamp=datetime.now(timezone.utc))
    try: e.set_thumbnail(url=user.display_avatar.url)
    except Exception: pass
    return brand_footer(e, '感謝您的支持')

def save_review(guild_id, user_id, rating, product, comment, channel_id='', message_id='', panel_id=''):
    q('INSERT INTO reviews(guild_id,user_id,rating,product,comment,channel_id,message_id,panel_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
      (str(guild_id), str(user_id), int(rating), str(product), str(comment or ''), str(channel_id), str(message_id), str(panel_id), now()))

def review_on_cooldown(guild_id, user_id, panel_id):
    r = q('SELECT created_at FROM reviews WHERE guild_id=? AND user_id=? AND panel_id=? ORDER BY id DESC LIMIT 1',
          (str(guild_id), str(user_id), str(panel_id)), True)
    if not r: return 0
    try: last = datetime.fromisoformat(r[0]['created_at'])
    except ValueError: return 0
    left = REVIEW_COOLDOWN_SEC - (datetime.now(timezone.utc) - last).total_seconds()
    return int(left) + 1 if left > 0 else 0

class ReviewCommentModal(discord.ui.Modal):
    def __init__(self, rating, product, panel_id):
        super().__init__(title=f'填寫評價（{rating} 星）'[:45], timeout=None)
        self.rating = rating; self.product = product; self.panel_id = panel_id
        self.comment = discord.ui.TextInput(label='評價內容（可留空）', style=discord.TextStyle.paragraph, required=False, max_length=900,
                                            placeholder='想說什麼都可以；不想寫直接送出，會顯示「無評價」')
        self.add_item(self.comment)

    async def on_submit(self, i):
        left = review_on_cooldown(i.guild.id, i.user.id, self.panel_id)
        if left:
            return await i.response.send_message(f'⏳ 您剛評價過這個商品，請 {max(1, left // 60)} 分鐘後再試。', ephemeral=True)
        await i.response.defer(ephemeral=True)
        text = str(self.comment.value or '').strip()
        target = i.channel
        cid = get(i.guild.id, 'review_channel', '')
        if cid:
            ch = i.guild.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel): target = ch
        msg_id = ''
        try:
            m = await target.send(embed=build_review_card(i.user, self.product, self.rating, text), allowed_mentions=discord.AllowedMentions.none())
            msg_id = m.id
        except (discord.Forbidden, discord.HTTPException) as ex:
            print(f'[REVIEW] 評價貼出失敗｜guild={i.guild.id}｜channel={getattr(target, "id", "?")}｜error={ex!r}')
        save_review(i.guild.id, i.user.id, self.rating, self.product, text, getattr(target, 'id', ''), msg_id, self.panel_id)
        await i.followup.send('✅ 感謝您的評價！' if msg_id else '✅ 已收到您的評價，感謝！（評價貼出失敗，請通知店家檢查評價頻道權限）', ephemeral=True)

def _review_star_callback(n):
    async def cb(i):
        if i.guild is None: return await i.response.send_message('❌ 只能在伺服器內使用。', ephemeral=True)
        product = panel_product_from_message(i.message) if i.message else ''
        if not product: return await i.response.send_message('❌ 這張評價面板沒有商品資料，請通知店家重新發送。', ephemeral=True)
        pid = i.message.id
        left = review_on_cooldown(i.guild.id, i.user.id, pid)
        if left: return await i.response.send_message(f'⏳ 您剛評價過這個商品，請 {max(1, left // 60)} 分鐘後再試。', ephemeral=True)
        await i.response.send_modal(ReviewCommentModal(n, product, pid))
    return cb

class ReviewPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for n in range(1, 6):
            b = discord.ui.Button(label=f'{n} 星', emoji='⭐', style=discord.ButtonStyle.primary, custom_id=f'{REVIEW_BUTTON_PREFIX}{n}')
            b.callback = _review_star_callback(n); self.add_item(b)

class ReviewPanelSettingsModal(discord.ui.Modal, title='設定評價面板'):
    def __init__(self, guild_id):
        super().__init__(); c = load_review_panel(guild_id); self.guild_id = guild_id
        self.t_title = discord.ui.TextInput(label='卡片標題', default=c['title'][:100], max_length=100)
        self.t_desc = discord.ui.TextInput(label='卡片說明', style=discord.TextStyle.paragraph, default=c['description'][:1000], max_length=1000)
        self.t_color = discord.ui.TextInput(label='顏色（6 碼，例如 F1C40F）', default=c['color'][:7], max_length=7, required=False)
        self.t_img = discord.ui.TextInput(label='圖片網址（選填，https 開頭）', default=c['image'][:400], max_length=400, required=False)
        for x in (self.t_title, self.t_desc, self.t_color, self.t_img): self.add_item(x)
    async def on_submit(self, i):
        img = str(self.t_img.value or '').strip()
        if img and not img.startswith(('http://', 'https://')):
            return await i.response.send_message('❌ 圖片網址要以 http:// 或 https:// 開頭。', ephemeral=True)
        col = str(self.t_color.value or '').strip().lstrip('#')
        if col and not re.fullmatch(r'[0-9a-fA-F]{6}', col):
            return await i.response.send_message('❌ 顏色請填 6 碼十六進位，例如 F1C40F。', ephemeral=True)
        setv(self.guild_id, 'review_panel', json.dumps({'title': str(self.t_title.value).strip(), 'description': str(self.t_desc.value).strip(),
             'color': col or 'F1C40F', 'image': img}, ensure_ascii=False))
        await i.response.send_message('✅ 面板外觀已儲存，預覽如下（之後新發送的面板會用這個樣子；已發出的面板要重新發送才會更新）：',
                                      embed=build_review_panel_embed(self.guild_id, '（發送面板時填的商品）'), ephemeral=True)

@bot.tree.command(name='發送評價面板', description='在頻道發送評價面板（大家都看得到，客人按星星評價）')
async def send_review_panel(i, 商品: str, 頻道: discord.TextChannel = None):
    if not admin(i.user): return await deny(i)
    product = 商品.strip()
    if not product: return await i.response.send_message('❌ 請填商品名稱。', ephemeral=True)
    ch = 頻道 or i.channel
    if not isinstance(ch, discord.TextChannel): return await i.response.send_message('❌ 請在文字頻道使用。', ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        await ch.send(embed=build_review_panel_embed(i.guild.id, product), view=ReviewPanelView())
    except discord.HTTPException as ex:
        return await i.followup.send(f'❌ 面板發送失敗：`{str(ex)[:150]}`（請確認機器人有該頻道的發送訊息／嵌入連結權限）', ephemeral=True)
    note = '' if get(i.guild.id, 'review_channel', '') else '\n💡 還沒設定評價頻道，評價會貼在客人按按鈕的那個頻道；用 `/設定評價頻道` 可指定。'
    await i.followup.send(f'✅ 評價面板已發送到 {ch.mention}（商品：{product}）。' + note, ephemeral=True)

@bot.tree.command(name='設定評價面板', description='自訂評價面板的小卡片（標題、說明、顏色、圖片）')
async def set_review_panel(i):
    if not admin(i.user): return await deny(i)
    await i.response.send_modal(ReviewPanelSettingsModal(i.guild.id))

@bot.tree.command(name='設定評價頻道', description='設定客人送出的評價要貼到哪個頻道')
async def set_review_channel(i, 頻道: discord.TextChannel):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id, 'review_channel', str(頻道.id))
    await i.response.send_message(f'✅ 評價會貼到 {頻道.mention}。', ephemeral=True)

@bot.tree.command(name='評價統計', description='查看評價數量與平均星數')
async def review_stats(i):
    if not admin(i.user): return await deny(i)
    r = q('SELECT COUNT(*) n, AVG(NULLIF(rating,0)) avg FROM reviews WHERE guild_id=?', (str(i.guild.id),), True)[0]
    if not r['n']: return await i.response.send_message('目前還沒有任何評價。', ephemeral=True)
    avg = f'{r["avg"]:.2f}' if r['avg'] is not None else '—'
    await i.response.send_message(f'📊 共 **{r["n"]}** 則評價，平均 **{avg}** 星。', ephemeral=True)

# 指令名稱直接使用繁體中文；Discord CHAT_INPUT 指令支援 Unicode 名稱。


@bot.event
async def on_ready():
    global TICKET_SYNCED, VIEWS_REGISTERED, PANEL_CLEANED
    if not TICKET_SYNCED:
        TICKET_CHANNEL_IDS.update(str(r['channel_id']) for r in q('SELECT channel_id FROM ticket_channels',(),True))
    if not VIEWS_REGISTERED:
        bot.add_view(OpenTicketView())
        bot.add_view(LegacyOpenTicketView())
        bot.add_view(WelcomeCloseView())
        bot.add_view(ServiceTypeView())
        bot.add_view(CoinProductView())
        bot.add_view(TicketControlView())
        bot.add_view(ClosedTicketView())
        bot.add_view(BoostTierView())
        bot.add_view(GiveawayView())
        bot.add_view(ReviewPanelView())
        for r in q("SELECT id FROM orders WHERE status='待付款'",(),True):
            bot.add_view(PaymentMethodView(int(r['id'])))
            bot.add_view(PaymentView(int(r['id'])))
        for r in q("SELECT id FROM orders WHERE status IN ('待確認付款','待排單','待交貨','處理中','待收貨')",(),True):
            bot.add_view(OrderManageView(int(r['id'])))
        VIEWS_REGISTERED=True
    # 每次程式啟動清理一次本 Bot 自己留下的舊互動面板；不碰其他 Bot。
    # （on_ready 在斷線重連時會再觸發，掃描要打大量 Discord 請求，所以只做一次。）
    if not PANEL_CLEANED:
        PANEL_CLEANED=True
        for g in bot.guilds:
            try:
                removed=await remove_obsolete_bot_panels(g)
                print(f'[PANEL_CLEANUP] {g.name} removed={removed}')
            except Exception as e:
                print('[PANEL_CLEANUP] 清理失敗:', repr(e))

    if not TICKET_SYNCED:
        sync_ok=True
        # 清除 Discord 上殘留的「全域」舊指令。指令已複製到各伺服器；
        # 若全域還留著舊版 /改名工單，輸入框會出現兩個同名指令，選到舊的就會跑出舊面板。
        try:
            await bot.http.bulk_upsert_global_commands(bot.application_id,[])
            print('[COMMAND_SYNC] 已清除全域舊指令')
        except Exception as e:
            print(f'[COMMAND_SYNC] 清除全域舊指令失敗｜error={e!r}')
        for g in bot.guilds:
            success=False
            for attempt in range(1,4):
                try:
                    # Slash commands are copied to each guild for immediate availability.
                    bot.tree.copy_global_to(guild=g)
                    synced = await bot.tree.sync(guild=g)
                    print(f'伺服器同步成功：{g.name} ({g.id})，{len(synced)} 個指令｜第 {attempt} 次')
                    success=True
                    break
                except (discord.HTTPException,discord.Forbidden,discord.NotFound) as e:
                    print(f'[COMMAND_SYNC] guild={g.id} attempt={attempt} failed｜error={e!r}')
                    if attempt < 3:
                        await asyncio.sleep(attempt * 1.5)
                except Exception as e:
                    print(f'[COMMAND_SYNC] guild={g.id} attempt={attempt} unexpected｜error={e!r}')
                    traceback.print_exc()
                    break
            if not success:
                sync_ok=False
        # 只在所有目前伺服器都成功時標記完成；失敗的下一次 on_ready 仍會重試。
        TICKET_SYNCED=sync_ok
    if not payment_reminder_loop.is_running(): payment_reminder_loop.start()
    if not db_backup_loop.is_running(): db_backup_loop.start()
    for g in bot.guilds:
        try:
            me=g.me or g.get_member(bot.user.id)
            if me:
                p=me.guild_permissions
                print(f'[BOT_PERMS] guild={g.name}({g.id}) administrator={p.administrator} manage_guild={p.manage_guild} manage_channels={p.manage_channels} manage_roles={p.manage_roles} manage_messages={p.manage_messages} view_channel={p.view_channel} send_messages={p.send_messages}')
        except Exception as e:
            print(f'[BOT_PERMS] guild={getattr(g,"id",None)} check failed: {e!r}')
    print('登入：',bot.user,'servers',len(bot.guilds))


# ---- 設定匯出／匯入 ----
CONFIG_EXPORT_VERSION = 2


def build_config_export(guild_id, include_accounts=False):
    settings_rows = q('SELECT key,value FROM settings WHERE key LIKE ?', (f'g:{guild_id}:%',), True)
    settings = {}
    prefix = f'g:{guild_id}:'
    for row in settings_rows:
        settings[row['key'][len(prefix):]] = row['value']
    product_rows = q('SELECT product,price,stock,enabled,availability_status FROM products ORDER BY product', (), True)
    products = [dict(r) for r in product_rows]
    boost_rows = q('SELECT tier,price FROM boost_prices WHERE guild_id=? ORDER BY tier', (str(guild_id),), True)
    boost_prices_data = [dict(r) for r in boost_rows]
    balance_rows = q('SELECT user_id,balance FROM balances ORDER BY user_id', (), True)
    balances_data = [dict(r) for r in balance_rows]
    cnt_rows = q('SELECT next_no FROM ticket_counters WHERE guild_id=?', (str(guild_id),), True)
    data = {'format':'CT小舖設定檔','version':CONFIG_EXPORT_VERSION,'exported_at':now(),'source_guild_id':str(guild_id),'settings':settings,'products':products,'boost_prices':boost_prices_data,'balances':balances_data,'ticket_counter':int(cnt_rows[0]['next_no']) if cnt_rows else 0,'meta':{'bot_config_schema':3,'notes':'Discord 頻道／身分組 ID 會隨伺服器不同而失效；會員餘額、下一個工單號會一併匯出／匯入。'}}
    if include_accounts:
        acc_rows = q("SELECT product,login_type,content FROM accounts WHERE guild_id=? AND status='available' ORDER BY id", (str(guild_id),), True)
        data['accounts'] = [dict(r) for r in acc_rows]
    return data


def config_bytes(guild_id, include_accounts=False):
    return json.dumps(build_config_export(guild_id, include_accounts), ensure_ascii=False, indent=2).encode('utf-8')


# 指令名稱直接使用繁體中文；Discord CHAT_INPUT 指令支援 Unicode 名稱。

@bot.tree.error
async def tree_error(i,e):
    # 統一處理全部 Slash Command 例外，避免只顯示「權限與參數」而看不到真正原因。
    command_name=getattr(getattr(i,'command',None),'qualified_name',None) or '未知指令'
    root=e.original if isinstance(e,app_commands.CommandInvokeError) and getattr(e,'original',None) else e
    print(f'[COMMAND_ERROR] command={command_name!r}｜type={type(root).__name__}｜error={root!r}')
    traceback.print_exception(type(root), root, root.__traceback__)

    if isinstance(root,app_commands.MissingPermissions):
        missing=', '.join(getattr(root,'missing_permissions',[]) or [])
        msg=f'❌ `/{command_name}` 使用權限不足。' + (f' Discord 要求：`{missing}`。' if missing else ' 請確認你有管理員身分組或必要的 Discord 管理權限。')
    elif isinstance(root,app_commands.CheckFailure):
        msg=f'❌ `/{command_name}` 目前沒有使用權限。'
    elif isinstance(root,app_commands.TransformerError):
        msg=f'❌ `/{command_name}` 的參數格式不正確，請重新選擇／輸入。'
    elif isinstance(root,discord.Forbidden):
        msg=f'❌ `/{command_name}` 被 Discord 拒絕：機器人缺少對應頻道／訊息／管理頻道權限。'
    elif isinstance(root,discord.NotFound):
        msg=f'❌ `/{command_name}` 找不到指定的 Discord 頻道、訊息或分類，可能已被刪除。'
    elif isinstance(root,discord.HTTPException):
        msg=f'❌ `/{command_name}` Discord API 暫時失敗（HTTP {getattr(root,"status","?")}）。請稍後再試。'
    elif isinstance(root,sqlite3.Error):
        msg=f'❌ `/{command_name}` 資料庫操作失敗：`{str(root)[:220]}`\n請看 Railway Log 的 `[COMMAND_ERROR]`。'
    elif isinstance(root,TypeError):
        msg=f'❌ `/{command_name}` 參數型別發生錯誤：`{str(root)[:220]}`\n請看 Railway Log 的 `[COMMAND_ERROR]`。'
    elif isinstance(root,AttributeError):
        msg=f'❌ `/{command_name}` 讀取資料時發生錯誤：`{str(root)[:220]}`\n請看 Railway Log 的 `[COMMAND_ERROR]`。'
    else:
        msg=f'❌ `/{command_name}` 執行失敗：`{type(root).__name__}: {str(root)[:220]}`\n請看 Railway Log 的 `[COMMAND_ERROR]`。'
    try:
        if i.response.is_done(): await i.followup.send(msg,ephemeral=True)
        else: await i.response.send_message(msg,ephemeral=True)
    except (discord.HTTPException,discord.NotFound):
        pass

if __name__=='__main__':
    if not TOKEN: raise SystemExit('請設定 DISCORD_TOKEN 環境變數。')
    bot.run(TOKEN)
