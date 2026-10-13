"""CT小舖自動測試。執行：python3 test_bot.py（不需要 Discord、不需要網路）。"""
import os, sys, tempfile, unittest, types, io, zipfile, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ── 讓測試在沒有安裝 discord.py 的環境也能 import bot.py：用萬用假模組取代 discord ──

class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith('__'): raise AttributeError(name)
        return Stub
    def __getitem__(cls, item): return Stub
    def __or__(cls, other): return Stub
    def __ror__(cls, other): return Stub

class Stub(metaclass=_Meta):
    def __init_subclass__(cls, **kw): pass
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k):
        # 當成 decorator 用時回傳原函式，不影響 bot.py 內的一般函式
        if len(a) == 1 and callable(a[0]) and not k: return a[0]
        return Stub()
    def __getattr__(self, name):
        if name.startswith('__'): raise AttributeError(name)
        return Stub()

def install():
    for name in ('discord', 'discord.ext', 'discord.ext.commands', 'discord.ext.tasks', 'discord.ui', 'discord.app_commands'):
        m = types.ModuleType(name)
        m.__getattr__ = lambda n, _s=Stub: _s
        sys.modules[name] = m
    sys.modules['discord'].ui = sys.modules['discord.ui']
    sys.modules['discord'].app_commands = sys.modules['discord.app_commands']
    sys.modules['discord.ext'].commands = sys.modules['discord.ext.commands']
    sys.modules['discord.ext'].tasks = sys.modules['discord.ext.tasks']
    # tasks.loop 要保留 .before_loop / .start 等屬性，所以回傳假物件而不是原函式
    sys.modules['discord.ext.tasks'].loop = lambda *a, **k: (lambda f: Stub())

install()
os.environ['DB_PATH'] = os.path.join(tempfile.mkdtemp(), 'test.db')
import bot
import asyncio as _aio
def _run_async(c): return _aio.run(c)

def reset():
    for t in ('orders','balances','balance_logs','products','accounts'): bot.q(f'DELETE FROM {t}')

def make_order(status='待付款', total=1000, service='幣號', buyer='111', reserved=0, qty=2, product='100M（普通）'):
    c = bot.db.cursor()
    c.execute('INSERT INTO orders(ticket_no,channel_id,guild_id,product,quantity,unit_price,total_price,status,buyer_id,created_at,service_type,stock_reserved) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
              ('0001','c1','g1',product,qty,total//qty,total,status,buyer,bot.now(),service,reserved))
    bot.db.commit(); return c.lastrowid

def set_bal(uid, n): bot.q('INSERT INTO balances(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance', (uid, n))
def bal(uid): return bot._balance_of(uid)

class BalancePay(unittest.TestCase):
    def setUp(self): reset()
    def test_pay_ok(self):
        set_bal('111', 1500); oid = make_order(total=1000)
        ok, msg, o = bot.pay_order_with_balance(oid, '111')
        self.assertTrue(ok, msg); self.assertEqual(bal('111'), 500)
        self.assertEqual(o['status'], '待交貨'); self.assertEqual(o['balance_paid'], 1000); self.assertEqual(o['payment_method'], '餘額')
        lg = bot.q('SELECT * FROM balance_logs',(),True); self.assertEqual(len(lg), 1); self.assertEqual(lg[0]['action'], '訂單付款'); self.assertEqual(lg[0]['balance_after'], 500)
    def test_boost_goes_to_queue(self):
        set_bal('111', 1000); oid = make_order(total=1000, service='代肝')
        ok, _, o = bot.pay_order_with_balance(oid, '111'); self.assertTrue(ok); self.assertEqual(o['status'], '待排單')
    def test_insufficient_no_deduction(self):
        set_bal('111', 999); oid = make_order(total=1000)
        ok, msg, _ = bot.pay_order_with_balance(oid, '111')
        self.assertFalse(ok); self.assertIn('還差 NT$1', msg); self.assertEqual(bal('111'), 999)
        self.assertEqual(bot.q('SELECT status FROM orders WHERE id=?',(oid,),True)[0]['status'], '待付款')
    def test_double_click_charges_once(self):
        set_bal('111', 5000); oid = make_order(total=1000)
        r1 = bot.pay_order_with_balance(oid, '111'); r2 = bot.pay_order_with_balance(oid, '111')
        self.assertTrue(r1[0]); self.assertFalse(r2[0]); self.assertEqual(bal('111'), 4000)
    def test_other_person_cannot_pay(self):
        set_bal('222', 5000); oid = make_order(total=1000, buyer='111')
        ok, _, _ = bot.pay_order_with_balance(oid, '222'); self.assertFalse(ok); self.assertEqual(bal('222'), 5000)
    def test_not_pending_cannot_pay(self):
        set_bal('111', 5000); oid = make_order(status='待交貨')
        self.assertFalse(bot.pay_order_with_balance(oid, '111')[0]); self.assertEqual(bal('111'), 5000)

class BalanceRefund(unittest.TestCase):
    def setUp(self): reset()
    def paid(self):
        set_bal('111', 1000); oid = make_order(total=1000); bot.pay_order_with_balance(oid, '111'); return oid
    def test_cancel_refunds_once(self):
        oid = self.paid(); self.assertEqual(bal('111'), 0)
        self.assertEqual(bot.refund_balance_on_cancel(oid, '待交貨', '已取消', 9), 1000); self.assertEqual(bal('111'), 1000)
        self.assertEqual(bot.refund_balance_on_cancel(oid, '待交貨', '已取消', 9), 0); self.assertEqual(bal('111'), 1000)
        self.assertEqual(bot.q("SELECT COUNT(*) n FROM balance_logs WHERE action='訂單退款'",(),True)[0]['n'], 1)
    def test_delivered_not_auto_refunded(self):
        oid = self.paid()
        self.assertEqual(bot.refund_balance_on_cancel(oid, '待收貨', '已取消', 9), 0); self.assertEqual(bal('111'), 0)
    def test_non_cancel_no_refund(self):
        oid = self.paid(); self.assertEqual(bot.refund_balance_on_cancel(oid, '待交貨', '處理中', 9), 0)
    def test_transfer_order_no_refund(self):
        oid = make_order(status='待交貨'); self.assertEqual(bot.refund_balance_on_cancel(oid, '待交貨', '已取消', 9), 0)

class StockRelease(unittest.TestCase):
    def setUp(self): reset(); bot.q("INSERT INTO products(product,stock) VALUES('100M（普通）',5) ON CONFLICT(product) DO UPDATE SET stock=5")
    def test_cancel_returns_stock_once(self):
        oid = make_order(reserved=1, qty=2)
        self.assertTrue(bot.release_reserved_stock(oid, '待付款', '已取消')); self.assertEqual(bot.stock('100M（普通）'), 7)
        self.assertFalse(bot.release_reserved_stock(oid, '待付款', '已取消')); self.assertEqual(bot.stock('100M（普通）'), 7)
    def test_delivered_keeps_stock(self):
        oid = make_order(reserved=1, qty=2); self.assertFalse(bot.release_reserved_stock(oid, '待收貨', '已取消')); self.assertEqual(bot.stock('100M（普通）'), 5)


class ReviewSystem(unittest.TestCase):
    def setUp(self): bot.q('DELETE FROM reviews'); bot.q("DELETE FROM settings WHERE key LIKE 'g:77:%'"); bot._SETTINGS_CACHE.clear()
    def test_card_text(self):
        t = bot.review_card_text(123, '100M 幣號', 4, '很快！')
        self.assertEqual(t, '◆ **感謝：**<@123> 光顧本店\n◆ **商品：**100M 幣號\n◆ **星級：**⭐⭐⭐⭐\n◆ **評價：**很快！')
    def test_blank_comment_is_no_review(self):
        for c in ('', '   ', None): self.assertTrue(bot.review_card_text(1, 'x', 5, c).endswith('◆ **評價：**無評價'))
    def test_stars_and_color(self):
        self.assertEqual(bot.review_stars(5), '⭐⭐⭐⭐⭐'); self.assertEqual(bot.review_stars(9), '⭐⭐⭐⭐⭐')
        self.assertEqual(bot.parse_review_color('#ff0000'), 0xFF0000); self.assertEqual(bot.parse_review_color('zzz'), 0xF1C40F)
    def test_product_from_panel_message(self):
        F = lambda n, v: types.SimpleNamespace(name=n, value=v)
        msg = types.SimpleNamespace(embeds=[types.SimpleNamespace(fields=[F('其他', 'a'), F('商品', '代肝 100M')])])
        self.assertEqual(bot.panel_product_from_message(msg), '代肝 100M')
        self.assertEqual(bot.panel_product_from_message(types.SimpleNamespace(embeds=[])), '')
    def test_panel_settings_defaults_and_saved(self):
        self.assertIn('星星', bot.load_review_panel(77)['description'])
        bot.setv(77, 'review_panel', '{"title":"我的評價","color":"FF0000","hack":"x"}')
        c = bot.load_review_panel(77); self.assertEqual((c['title'], c['color']), ('我的評價', 'FF0000')); self.assertNotIn('hack', c)
        bot.setv(77, 'review_panel', 'not json'); self.assertEqual(bot.load_review_panel(77)['color'], 'F1C40F')
    def test_cooldown_is_per_user_per_panel(self):
        self.assertEqual(bot.review_on_cooldown(77, 5, 900), 0)
        bot.save_review(77, 5, 4, '商品A', '好', 1, 2, 900)
        self.assertGreater(bot.review_on_cooldown(77, 5, 900), 0)
        self.assertEqual(bot.review_on_cooldown(77, 5, 901), 0)      # 另一張面板（別的商品）可以再評
        self.assertEqual(bot.review_on_cooldown(77, 6, 900), 0)      # 別人不受影響
        r = bot.q('SELECT rating,product,comment FROM reviews', (), True)[0]; self.assertEqual((r['rating'], r['product'], r['comment']), (4, '商品A', '好'))

class Helpers(unittest.TestCase):
    def test_safe_format(self):
        self.assertEqual(bot.safe_format('銀行 {bank} {x} {} JSON{"a":1}', bank='中信'), '銀行 中信 {x} {} JSON{"a":1}')
    def test_db_snapshot_is_valid_zip_with_data(self):
        import zipfile, io, sqlite3
        reset(); set_bal('111', 321)
        z = zipfile.ZipFile(io.BytesIO(bot.db_snapshot_zip())); p = os.path.join(tempfile.mkdtemp(), 'x.db'); open(p, 'wb').write(z.read('trade_bot.db'))
        self.assertEqual(sqlite3.connect(p).execute("SELECT balance FROM balances WHERE user_id='111'").fetchone()[0], 321)
    def test_low_stock_threshold_default_and_off(self):
        class G: id = 1
        self.assertEqual(bot.low_stock_threshold(1), 3); bot.setv(1, 'low_stock_threshold', 0); self.assertEqual(bot.low_stock_threshold(1), 0)

class ShopPause(unittest.TestCase):
    def test_pause_flag(self):
        bot.setv(7, 'shop_status', '營業中'); self.assertFalse(bot.shop_paused(7))
        bot.setv(7, 'shop_status', '休息中'); self.assertFalse(bot.shop_paused(7))   # 休息中只是標示，不擋下單
        bot.setv(7, 'shop_status', '暫停接單'); self.assertTrue(bot.shop_paused(7))
    def test_paused_message_includes_notice_and_custom_text(self):
        bot.setv(8, 'rest_text', '今晚 10 點回來'); self.assertIn('今晚 10 點回來', bot.paused_message(8))
        bot.setv(8, 'paused_text', '休息一下'); self.assertTrue(bot.paused_message(8).startswith('休息一下'))
    def test_restock_line(self):
        self.assertEqual(bot.restock_line(9, 'X'), ''); bot.setv(9, 'restock_X', '週五晚上'); self.assertIn('週五晚上', bot.restock_line(9, 'X'))

class FakeMember(bot.discord.Member):
    def __init__(self, uid, roles=(), admin=False, is_bot=False, gid=50):
        self.id = uid; self.bot = is_bot; self.display_name = f'u{uid}'
        self.guild = types.SimpleNamespace(id=gid)
        self.guild_permissions = types.SimpleNamespace(administrator=admin, manage_guild=False)
        self.roles = [types.SimpleNamespace(id=1000 + n, name=r) for n, r in enumerate(roles)]

class BuyerDetection(unittest.TestCase):
    def test_shop_owner_is_never_the_customer(self):
        bot.setv(50, 'ticket_owner_id', '7')
        self.assertTrue(bot._is_staff_member(FakeMember(7)))                 # 設定的店長
        self.assertFalse(bot._is_staff_member(FakeMember(8)))                # 一般客人
    def test_boss_role_names_are_staff(self):
        for name in ('店長', '店長👑', '管理員'):
            self.assertTrue(bot._is_staff_member(FakeMember(9, roles=(name,))), name)
    def test_customer_role_laoban_is_not_staff_and_not_admin(self):
        """「闆闆」是客人身分組：有這個身分組的人不能被當成店內人員，更不能是管理員。"""
        for name in ('闆闆', '闆闆👑'):
            m = FakeMember(9, roles=(name,))
            self.assertFalse(bot._is_staff_member(m), name); self.assertFalse(bot.admin(m), name); self.assertFalse(bot.strict_admin(m), name)
    def test_admin_permission_and_bots_are_staff(self):
        self.assertTrue(bot._is_staff_member(FakeMember(10, admin=True))); self.assertTrue(bot._is_staff_member(FakeMember(11, is_bot=True)))

class ForeignTicketGuard(unittest.TestCase):
    def setUp(self):
        reset(); bot.q('DELETE FROM ticket_channels'); bot.setv(50, 'ticket_owner_id', '')
        bot.q("INSERT INTO ticket_channels(channel_id,guild_id,ticket_no,first_name,buyer_id,buyer_name,detected_at) VALUES('c9','50','0009','x','100','客人A',?)", (bot.now(),))
    def inter(self, user, members=()):
        users = {m.id: m for m in members}
        return types.SimpleNamespace(user=user, guild=types.SimpleNamespace(id=50, get_member=lambda uid: users.get(uid)))
    ch = types.SimpleNamespace(id='c9')
    def test_other_person_is_blocked(self):
        self.assertIn('<@100>', bot.foreign_ticket_notice(self.inter(FakeMember(200)), self.ch))
    def test_real_buyer_ok(self):
        self.assertIsNone(bot.foreign_ticket_notice(self.inter(FakeMember(100)), self.ch))
    def test_non_ticket_channel_ok(self):
        self.assertIsNone(bot.foreign_ticket_notice(self.inter(FakeMember(200)), types.SimpleNamespace(id='nope')))
    def test_self_heal_when_recorded_buyer_was_staff(self):
        boss = FakeMember(100, roles=('店長',)); customer = FakeMember(200)
        self.assertIsNone(bot.foreign_ticket_notice(self.inter(customer, members=[boss]), self.ch))
        self.assertEqual(bot.q("SELECT buyer_id FROM ticket_channels WHERE channel_id='c9'", (), True)[0]['buyer_id'], '200')


class SplitAccounts(unittest.TestCase):
    def test_em_dash_inline(self):
        self.assertEqual(bot.split_account_blocks('a----1 ————— b----2 ————— c----3'),['a----1','b----2','c----3'])
    def test_em_dash_lines_multiline_blocks(self):
        t='帳號：a\n密碼：1\n—————\n帳號：b\n密碼：2\n—————\n'
        self.assertEqual(bot.split_account_blocks(t),['帳號：a\n密碼：1','帳號：b\n密碼：2'])
    def test_ascii_line_separator(self):
        self.assertEqual(bot.split_account_blocks('a----1\n-----\nb----2'),['a----1','b----2'])
    def test_no_separator_one_per_line(self):
        self.assertEqual(bot.split_account_blocks('a----1\n\nb----2\n'),['a----1','b----2'])
    def test_field_separator_not_split(self):
        self.assertEqual(bot.split_account_blocks('帳號：a----密碼：1'),['帳號：a----密碼：1'])
    def test_labels(self):
        self.assertEqual(bot._lt_label(''),'未指定登入'); self.assertIn('',bot.POOL_TYPES)


class AccountFormat(unittest.TestCase):
    def test_one_group_one_block(self):
        c = '郵件：rc8@x.com\n密碼：pw1\n備用郵件：b@o.com\n密碼：pw1'
        md = bot._account_md(c, 1)
        self.assertEqual(md, '**【第 1 組】**\n```\n' + c + '\n```')
        self.assertEqual(md.count('```'), 2)
    def test_no_title_when_single(self):
        self.assertEqual(bot._account_md('帳號：u\n密碼：p'), '```\n帳號：u\n密碼：p\n```')
    def test_inline_fields_become_lines(self):
        self.assertEqual(bot._account_md('帳號：a----密碼：b'), '```\n帳號：a\n密碼：b\n```')
    def test_triple_backtick_inside_does_not_break_block(self):
        self.assertEqual(bot._account_md('a```b').count('```'), 2)


class DumpCommand(unittest.TestCase):
    def test_registered_and_sorted_helpers(self):
        self.assertTrue(callable(bot.account_stock_dump))
    def test_file_roundtrip_blocks(self):
        # 檔案內容（不含標題行）用 ————— 分開，能被 split_account_blocks 還原成原本的帳號
        body = '\n—————\n'.join(['u1', '帳號：u2\n密碼：pw2'])
        self.assertEqual(bot.split_account_blocks(body), ['u1', '帳號：u2\n密碼：pw2'])


class Giveaway(unittest.TestCase):
    def setUp(self):
        bot.q('DELETE FROM giveaways'); bot.q('DELETE FROM giveaway_entries')
    def _g(self, winners=1):
        c = bot.db.cursor()
        c.execute('INSERT INTO giveaways(guild_id,channel_id,prize,winners,host_id,created_at) VALUES(?,?,?,?,?,?)', ('g1','c1','100M',winners,'h',bot.now()))
        bot.db.commit(); return c.lastrowid
    def test_pick_count_and_unique(self):
        r = bot.giveaway_pick(['1','2','3','4','5'], 3)
        self.assertEqual(len(r), 3); self.assertEqual(len(set(r)), 3)
    def test_pick_more_than_entries(self):
        self.assertEqual(sorted(bot.giveaway_pick(['1','2'], 5)), ['1','2'])
    def test_pick_empty(self):
        self.assertEqual(bot.giveaway_pick([], 3), [])
    def test_pick_excludes_old_winners(self):
        for _ in range(30):
            self.assertEqual(bot.giveaway_pick(['1','2','3'], 1, exclude=['1','2']), ['3'])
    def test_pick_dedupes_entries(self):
        self.assertEqual(bot.giveaway_pick(['1','1','1'], 3), ['1'])
    def test_one_entry_per_user(self):
        gid = self._g(); c = bot.db.cursor()
        for _ in range(3): c.execute('INSERT OR IGNORE INTO giveaway_entries(giveaway_id,user_id,joined_at) VALUES(?,?,?)', (gid,'9',bot.now()))
        bot.db.commit(); self.assertEqual(bot.giveaway_count(gid), 1)
    def test_pick_valid_uses_fetch_when_not_cached(self):
        import asyncio
        class G:
            def get_member(self, uid): return None            # 快取空的（沒開成員權限）
            async def fetch_member(self, uid): return object()  # 但 Discord 查得到
        w, gone = asyncio.run(bot.giveaway_pick_valid(G(), ['1','2','3'], 2))
        self.assertEqual(len(w), 2); self.assertEqual(gone, 0)
    def test_pick_valid_skips_left_members(self):
        import asyncio
        class G:
            def get_member(self, uid): return object() if uid == 3 else None
            async def fetch_member(self, uid): raise LookupError
        async def run():
            orig = bot._giveaway_still_in_guild
            async def fake(guild, uid): return str(uid) == '3'
            bot._giveaway_still_in_guild = fake
            try: return await bot.giveaway_pick_valid(G(), ['1','2','3'], 1)
            finally: bot._giveaway_still_in_guild = orig
        w, gone = asyncio.run(run())
        self.assertEqual(w, ['3'])
    def test_pick_valid_excludes_old_winners(self):
        import asyncio
        class G:
            def get_member(self, uid): return object()
        w, _ = asyncio.run(bot.giveaway_pick_valid(G(), ['1','2'], 5, exclude=['1']))
        self.assertEqual(w, ['2'])
    def test_count_refresh_is_coalesced(self):
        import asyncio
        gid = self._g(); calls = []
        async def fake(guild, g, **kw): calls.append(g['id'])
        async def run():
            orig = bot._giveaway_refresh_message; old_delay = bot.GIVEAWAY_REFRESH_DELAY
            bot._giveaway_refresh_message = fake; bot.GIVEAWAY_REFRESH_DELAY = 0.01
            try:
                for _ in range(5): bot.giveaway_schedule_refresh(None, gid)
                await asyncio.sleep(0.1)
                bot.giveaway_schedule_refresh(None, gid); await asyncio.sleep(0.1)
            finally: bot._giveaway_refresh_message = orig; bot.GIVEAWAY_REFRESH_DELAY = old_delay
        asyncio.run(run())
        self.assertEqual(calls, [gid, gid])      # 5 次合併成 1 次；之後再有人按又會更新
        self.assertEqual(bot._GIVEAWAY_REFRESH_PENDING, {})
    def test_count_refresh_skips_ended(self):
        import asyncio
        gid = self._g(); bot.q("UPDATE giveaways SET status='ended' WHERE id=?", (gid,)); calls = []
        async def fake(guild, g, **kw): calls.append(1)
        async def run():
            orig = bot._giveaway_refresh_message; old_delay = bot.GIVEAWAY_REFRESH_DELAY
            bot._giveaway_refresh_message = fake; bot.GIVEAWAY_REFRESH_DELAY = 0.01
            try: bot.giveaway_schedule_refresh(None, gid); await asyncio.sleep(0.1)
            finally: bot._giveaway_refresh_message = orig; bot.GIVEAWAY_REFRESH_DELAY = old_delay
        asyncio.run(run()); self.assertEqual(calls, [])
    def test_embed_builds(self):
        gid = self._g(2); bot.giveaway_embed(bot.giveaway_get(gid)); bot.giveaway_embed(bot.giveaway_get(gid), ended=True, winner_ids=['1'])
        bot.giveaway_embed(bot.giveaway_get(gid), ended=True, cancelled=True)
    def test_embed_shows_live_count(self):
        gid = self._g(); c = bot.db.cursor()
        for u in ('1','2','3'): c.execute('INSERT INTO giveaway_entries(giveaway_id,user_id,joined_at) VALUES(?,?,?)', (gid,u,bot.now()))
        bot.db.commit(); self.assertEqual(bot.giveaway_count(gid), 3)



class RenameDoesNotMove(unittest.TestCase):
    """改名工單／修改狀態改成「結單」或重新開放，只改名稱與權限，不能移動頻道。"""
    def _run(self, old, new):
        import asyncio
        class Ch:
            id = 1; category_id = 5
            def __init__(self): self.edits = []
            async def edit(self, **kw): self.edits.append(kw)
        ch = Ch(); calls = {'moved': 0, 'restored': 0}
        async def fake_move(*a, **k): calls['moved'] += 1; return True
        async def fake_restore(*a, **k): calls['restored'] += 1; return ([], [])
        o1, o2 = bot.move_closed_ticket_to_service_category, bot.restore_access
        bot.move_closed_ticket_to_service_category, bot.restore_access = fake_move, fake_restore
        try: out = asyncio.run(bot.sync_ticket_state_after_rename(ch, old, new, '9', '幣號'))
        finally: bot.move_closed_ticket_to_service_category, bot.restore_access = o1, o2
        return ch, calls, out
    def test_rename_to_closed_does_not_move(self):
        ch, calls, out = self._run('待處理', '結單')
        self.assertEqual(ch.edits, []); self.assertEqual(calls['moved'], 0); self.assertIn('沒有移動', out)
    def test_reopen_does_not_move_but_restores_access(self):
        ch, calls, out = self._run('結單', '待處理')
        self.assertEqual(ch.edits, []); self.assertEqual(calls['moved'], 0); self.assertEqual(calls['restored'], 1)
    def test_other_status_changes_are_noop(self):
        ch, calls, out = self._run('待付款', '待處理')
        self.assertEqual((ch.edits, calls['moved'], calls['restored'], out), ([], 0, 0, ''))

class RemovedCommands(unittest.TestCase):
    def test_removed_commands_stay_removed(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.py'), encoding='utf-8') as f: src = f.read()
        self.assertNotIn("name='結單'", src)
        self.assertNotIn("name='設定真人客服訊息'", src)
        self.assertNotIn('ticket_welcome_human', src)
        for dead in ('panel_text', 'order_confirm_text', 'delivery_accept_text'): self.assertNotIn(dead, src)

class ImportDatabase(unittest.TestCase):
    def _file(self, data):
        p = os.path.join(tempfile.mkdtemp(), 'in.db')
        with open(p, 'wb') as f: f.write(data)
        return p
    def test_roundtrip_restores_data_and_clears_caches(self):
        set_bal('111', 500)
        data = bot.db_bytes_from_upload(bot.db_snapshot_zip())          # /匯出資料庫 的 zip
        p = self._file(data)
        info = bot.inspect_db_file(p, 'g1'); self.assertGreaterEqual(info['balances'], 1)
        set_bal('111', 900); bot.setv('g1', 'zz_test_key', '1')
        bot.restore_live_db(p)
        self.assertEqual(bot.q("SELECT balance FROM balances WHERE user_id='111'", (), True)[0]['balance'], 500)
        self.assertEqual(bot.get('g1', 'zz_test_key', 'NONE'), 'NONE')    # 快取已清，讀到還原後的資料
        bot.setv('g1', 'after_restore', '1'); self.assertEqual(bot.get('g1', 'after_restore'), '1')   # 還原後仍可正常寫入
    def test_plain_db_also_accepted(self):
        z = zipfile.ZipFile(io.BytesIO(bot.db_snapshot_zip())); raw = z.read('trade_bot.db')
        self.assertEqual(bot.db_bytes_from_upload(raw), raw)
    def test_old_backup_missing_new_tables_gets_upgraded(self):
        p = self._file(bot.db_bytes_from_upload(bot.db_snapshot_zip()))
        c = sqlite3.connect(p); c.execute('DROP TABLE giveaways'); c.execute('DROP TABLE giveaway_entries'); c.commit(); c.close()
        bot.restore_live_db(p)
        self.assertEqual(bot.q('SELECT COUNT(*) AS n FROM giveaways', (), True)[0]['n'], 0)    # init_db 補回新資料表
    def test_rejects_non_sqlite(self):
        with self.assertRaises(ValueError): bot.inspect_db_file(self._file(b'hello world, not a database' * 50), 'g1')
    def test_rejects_other_sqlite_without_our_tables(self):
        p = self._file(b''); c = sqlite3.connect(p); c.execute('CREATE TABLE x(a)'); c.commit(); c.close()
        with self.assertRaises(ValueError): bot.inspect_db_file(p, 'g1')
    def test_zip_without_db_and_bad_zip_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z: z.writestr('readme.txt', 'x')
        with self.assertRaises(ValueError): bot.db_bytes_from_upload(buf.getvalue())
        with self.assertRaises(ValueError): bot.db_bytes_from_upload(b'PK\x03\x04 broken')
    def test_failed_inspect_leaves_live_db_untouched(self):
        set_bal('222', 77)
        with self.assertRaises(ValueError): bot.inspect_db_file(self._file(b'garbage' * 100), 'g1')
        self.assertEqual(bot.q("SELECT balance FROM balances WHERE user_id='222'", (), True)[0]['balance'], 77)


class ImportDatabaseCommand(unittest.TestCase):
    """把 /匯入資料庫 指令本身端到端跑一遍（用假的 Discord 物件）。"""
    def _env(self, payload, confirm):
        import asyncio
        sent = []
        class R:
            async def defer(self, **k): pass
        class F:
            async def send(self, msg='', **k): sent.append((msg, k))
        class Att:
            size = len(payload)
            async def read(self): return payload
        class I:
            user = object(); guild = types.SimpleNamespace(id='g1', name='shop'); response = R(); followup = F()
        old = (bot.strict_admin, getattr(bot.discord, 'HTTPException', None))
        bot.strict_admin = lambda m: True
        bot.discord.HTTPException = type('HTTPException', (Exception,), {})
        try: asyncio.run(bot.import_database(I(), Att(), confirm))
        finally: bot.strict_admin = old[0]; bot.discord.HTTPException = old[1]
        return sent
    def test_preview_does_not_change_anything(self):
        set_bal('333', 10); zipped = bot.db_snapshot_zip(); set_bal('333', 20)
        sent = self._env(zipped, '')
        self.assertIn('還沒有還原', sent[0][0]); self.assertIn('會員餘額', sent[0][0])
        self.assertEqual(bot.q("SELECT balance FROM balances WHERE user_id='333'", (), True)[0]['balance'], 20)
    def test_wrong_confirm_text_does_not_restore(self):
        set_bal('333', 10); zipped = bot.db_snapshot_zip(); set_bal('333', 20)
        sent = self._env(zipped, '確認')
        self.assertIn('還沒有還原', sent[0][0])
        self.assertEqual(bot.q("SELECT balance FROM balances WHERE user_id='333'", (), True)[0]['balance'], 20)
    def test_confirm_restores_and_sends_safety_backup(self):
        set_bal('333', 10); zipped = bot.db_snapshot_zip(); set_bal('333', 20)
        sent = self._env(zipped, '確認覆蓋')
        self.assertIn('已還原', sent[0][0]); self.assertIn('file', sent[0][1])        # 附上「還原前」的備份
        self.assertEqual(bot.q("SELECT balance FROM balances WHERE user_id='333'", (), True)[0]['balance'], 10)
    def test_garbage_upload_is_rejected_and_data_kept(self):
        set_bal('333', 20)
        sent = self._env(b'not a database at all' * 20, '確認覆蓋')
        self.assertIn('❌', sent[0][0])
        self.assertEqual(bot.q("SELECT balance FROM balances WHERE user_id='333'", (), True)[0]['balance'], 20)
    def test_temp_file_cleaned_up(self):
        self._env(bot.db_snapshot_zip(), '')
        self.assertFalse(os.path.exists(bot.DB_PATH + '.import.tmp'))


class AccountScan(unittest.TestCase):
    """掃描工單訊息：找「名稱：／郵件：」開頭的帳號，第二次出現就提醒重複。"""
    def setUp(self):
        reset(); bot.q('DELETE FROM account_msg_log'); bot.q('DELETE FROM account_scan_state')
        self._old = (getattr(bot.discord, 'HTTPException', None), getattr(bot.discord, 'Forbidden', None), getattr(bot.discord, 'NotFound', None), bot.discord.TextChannel)
        class HTTPException(Exception): pass
        class Forbidden(HTTPException): pass
        class NotFound(HTTPException): pass
        bot.discord.HTTPException, bot.discord.Forbidden, bot.discord.NotFound = HTTPException, Forbidden, NotFound
        self._old_obj = bot.discord.Object; bot.discord.Object = lambda id: types.SimpleNamespace(id=id)
        self.logs = []
        self._old_log = bot.post_channel_log
        async def fake_log(guild, key, text): self.logs.append(text)
        bot.post_channel_log = fake_log
    def tearDown(self):
        bot.discord.HTTPException, bot.discord.Forbidden, bot.discord.NotFound, bot.discord.TextChannel = self._old
        bot.post_channel_log = self._old_log; bot.discord.Object = self._old_obj; bot.TICKET_CHANNEL_IDS.clear()
    # ── 解析 ──
    def test_extract_user_formats(self):
        self.assertEqual(bot.extract_account_entries('郵件：rc8@x.com\n密碼：pw\n備用郵件：b@o.com\n密碼：pw'), [('mail', 'rc8@x.com')])
        self.assertEqual(bot.extract_account_entries('名稱：Google登入 100M\n郵件：a@x.com'), [('mail', 'a@x.com')])
        self.assertEqual(bot.extract_account_entries('名稱：小明'), [('name', '小明')])
    def test_extract_with_discord_decoration_and_headers(self):
        self.assertEqual(bot.extract_account_entries('**【第 1 組】**\n```\n郵件：a@x.com\n密碼：p\n```'), [('mail', 'a@x.com')])
    def test_extract_multiple_accounts_in_one_message(self):
        self.assertEqual(bot.extract_account_entries('郵件：a@x.com\n密碼：1\n\n郵件：b@x.com\n密碼：2'), [('mail', 'a@x.com'), ('mail', 'b@x.com')])
        self.assertEqual(bot.extract_account_entries('名稱：n1\n郵件：a@x.com\n—————\n名稱：n2\n郵件：b@x.com'), [('mail', 'a@x.com'), ('mail', 'b@x.com')])
    def test_extract_only_messages_that_start_with_label(self):
        for t in ('好的 郵件：a@x.com', '這是帳號\n郵件：a@x.com', '備用郵件：b@o.com', '密碼：123', '郵件：', '', '你好'):
            self.assertEqual(bot.extract_account_entries(t), [], t)
    def test_fingerprint_ignores_case(self):
        self.assertEqual(bot.account_fingerprint('g', 'mail', 'A@X.com'), bot.account_fingerprint('g', 'mail', 'a@x.com'))
        self.assertNotEqual(bot.account_fingerprint('g', 'mail', 'a@x.com'), bot.account_fingerprint('g', 'name', 'a@x.com'))
    # ── 即時偵測 ──
    def _msg(self, mid, channel_id, content, bot_author=True):
        replies = []
        class Ch:
            id = channel_id
            async def send(self, text='', **k): replies.append(text)
        m = types.SimpleNamespace(id=mid, content=content, channel=Ch(), guild=types.SimpleNamespace(id='g1'), author=types.SimpleNamespace(id=7, bot=bot_author, mention='<@7>'), created_at='2026-10-06 12:00:00')
        async def reply(text, **k): replies.append(text)
        m.reply = reply
        return m, replies
    def _live(self, mid, channel_id, content):
        import asyncio
        m, replies = self._msg(mid, channel_id, content); asyncio.run(bot.account_scan_live(m)); return replies
    def test_first_silent_second_warns_third_counts(self):
        self.assertEqual(self._live(100, 'c1', '郵件：A@x.com\n密碼：secret1'), [])
        r2 = self._live(200, 'c2', '郵件：a@x.com\n密碼：secret1')
        self.assertEqual(len(r2), 1); self.assertIn('偵測到重複', r2[0]); self.assertIn('第 2 次', r2[0]); self.assertNotIn('secret1', r2[0])
        r3 = self._live(300, 'c3', '郵件：a@x.com'); self.assertIn('第 3 次', r3[0])
        self.assertEqual(len(self.logs), 2)
    def test_different_accounts_not_flagged(self):
        self._live(100, 'c1', '郵件：a@x.com'); self.assertEqual(self._live(200, 'c2', '郵件：b@x.com'), [])
    def test_same_message_twice_account_is_flagged(self):
        r = self._live(100, 'c1', '郵件：a@x.com\n密碼：1\n\n郵件：a@x.com\n密碼：2'); self.assertEqual(len(r), 1)
    def test_chat_messages_never_flagged(self):
        self._live(100, 'c1', '好的 郵件：a@x.com'); self.assertEqual(self._live(200, 'c2', '好的 郵件：a@x.com'), [])
    def test_deleted_message_is_forgotten(self):
        import asyncio
        self._live(100, 'c1', '郵件：a@x.com')
        asyncio.run(bot.on_raw_message_delete(types.SimpleNamespace(message_id=100)))     # 發錯工單，刪掉
        self.assertEqual(self._live(200, 'c2', '郵件：a@x.com'), [])
    def test_resend_of_same_message_does_not_double_count(self):
        self._live(100, 'c1', '郵件：a@x.com'); self.assertEqual(self._live(100, 'c1', '郵件：a@x.com'), [])
    def test_on_message_hook_includes_bot_authored_messages(self):
        import asyncio
        class FakeTextChannel: pass
        bot.discord.TextChannel = FakeTextChannel
        bot.TICKET_CHANNEL_IDS.update({'c1', 'c2'})
        out = []
        def mk(mid, cid, text):
            ch = FakeTextChannel(); ch.id = cid
            async def send(t='', **k): out.append(t)
            ch.send = send
            m = types.SimpleNamespace(id=mid, content=text, channel=ch, guild=types.SimpleNamespace(id='g1'), author=types.SimpleNamespace(id=9, bot=True), created_at='2026-10-06')
            async def reply(t, **k): out.append(t)
            m.reply = reply; return m
        async def feed(m):
            await bot.on_message(m)
            while bot._BG_TASKS: await asyncio.gather(*list(bot._BG_TASKS))     # 偵測在背景跑，等它跑完再檢查
        asyncio.run(feed(mk(10, 'c1', '郵件：z@x.com\n密碼：1'))); self.assertEqual(out, [])
        asyncio.run(feed(mk(20, 'c2', '郵件：z@x.com\n密碼：1'))); self.assertEqual(len(out), 1); self.assertIn('偵測到重複', out[0])
    def test_detection_runs_in_background_and_errors_never_escape(self):
        """偵測出錯或很慢，on_message 本身都要照樣回來，不能影響後面的處理。"""
        import asyncio
        class FakeTextChannel: pass
        bot.discord.TextChannel = FakeTextChannel; bot.TICKET_CHANNEL_IDS.add('c1')
        ch = FakeTextChannel(); ch.id = 'c1'
        m = types.SimpleNamespace(id=1, content='郵件：a@x.com', channel=ch, guild=types.SimpleNamespace(id='g1'), author=types.SimpleNamespace(id=9, bot=True), created_at='x')
        async def boom(msg): raise RuntimeError('boom')
        async def slow(msg): await asyncio.sleep(5)
        old = bot.account_scan_live
        try:
            for fn in (boom, slow):
                bot.account_scan_live = fn
                async def run():
                    t0 = asyncio.get_event_loop().time(); await bot.on_message(m)
                    took = asyncio.get_event_loop().time() - t0
                    for t in list(bot._BG_TASKS): t.cancel()
                    return took
                self.assertLess(asyncio.run(run()), 1.0)      # on_message 不會等偵測
        finally: bot.account_scan_live = old
    # ── 補掃 ──
    def _guild(self, spec):
        """spec: {channel_id: [(message_id, content), ...]}；回傳 (guild, 回覆紀錄)"""
        replies = []
        class FakeTextChannel: pass
        bot.discord.TextChannel = FakeTextChannel
        chans = {}
        for cid, msgs in spec.items():
            ch = FakeTextChannel(); ch.id = int(cid)
            def hist(msgs=msgs, **kw):
                async def gen():
                    after = kw.get('after'); aid = getattr(after, 'id', 0) if after else 0
                    for mid, text in msgs:
                        if mid > aid: yield types.SimpleNamespace(id=mid, content=text, author=types.SimpleNamespace(id=1), created_at='2026-10-06')
                return gen()
            ch.history = hist
            def gpm(mid, cid=cid):
                async def reply(t, **k): replies.append((cid, mid, t))
                return types.SimpleNamespace(reply=reply)
            ch.get_partial_message = gpm
            async def send(t='', cid=cid, **k): replies.append((cid, None, t))
            ch.send = send
            chans[int(cid)] = ch
        guild = types.SimpleNamespace(id='g1', get_channel=lambda x: chans.get(int(x)))
        bot.TICKET_CHANNEL_IDS.update(str(c) for c in chans)
        return guild, replies
    def test_scan_warns_second_and_later_in_chronological_order_once(self):
        import asyncio
        g, rp = self._guild({'11': [(100, '郵件：x@y.com\n密碼：1')], '22': [(200, '郵件：x@y.com')], '33': [(150, '郵件：X@Y.com')], '44': [(300, '郵件：other@y.com')]})
        rep = asyncio.run(bot.scan_ticket_accounts(g, announce=True))
        self.assertEqual(rep['channels'], 4); self.assertEqual(rep['messages'], 4)
        self.assertEqual(sorted((c, m) for c, m, _ in rp), [('22', 200), ('33', 150)])      # 最早的 100 不提醒；150 是第 2 次、200 是第 3 次
        txt = {m: t for _, m, t in rp}; self.assertIn('第 2 次', txt[150]); self.assertIn('第 3 次', txt[200])
        rp.clear(); asyncio.run(bot.scan_ticket_accounts(g, announce=True)); self.assertEqual(rp, [])     # 再掃一次不重複提醒
    def test_scan_without_announce_only_reports(self):
        import asyncio
        g, rp = self._guild({'11': [(100, '郵件：x@y.com')], '22': [(200, '郵件：x@y.com')]})
        rep = asyncio.run(bot.scan_ticket_accounts(g, announce=False))
        self.assertEqual(rp, []); self.assertEqual(len(rep['dups']), 1)
        rep2 = asyncio.run(bot.scan_ticket_accounts(g, announce=True)); self.assertEqual(len(rp), 1)    # 之後選要提醒，才會發
    def test_incremental_scan_only_reads_new_messages(self):
        import asyncio
        spec = {'11': [(100, '郵件：x@y.com')]}
        g, rp = self._guild(spec)
        asyncio.run(bot.scan_ticket_accounts(g)); spec['11'].append((200, '郵件：x@y.com'))
        rep = asyncio.run(bot.scan_ticket_accounts(g)); self.assertEqual(rep['messages'], 1); self.assertEqual(len(rp), 1)
    def test_full_rescan_forgets_deleted_messages(self):
        import asyncio
        spec = {'11': [(100, '郵件：x@y.com')], '22': [(200, '郵件：x@y.com')]}
        g, rp = self._guild(spec)
        asyncio.run(bot.scan_ticket_accounts(g, announce=False)); spec['11'].clear()          # 100 這則被刪了
        rep = asyncio.run(bot.scan_ticket_accounts(g, announce=False, full=True)); self.assertEqual(rep['dups'], [])
    def test_unreadable_channel_is_skipped_not_fatal(self):
        import asyncio
        g, rp = self._guild({'11': [(100, '郵件：x@y.com')]})
        def boom(**kw):
            async def gen():
                raise bot.discord.Forbidden('no access'); yield
            return gen()
        g.get_channel(11).history = boom
        rep = asyncio.run(bot.scan_ticket_accounts(g)); self.assertEqual((rep['channels'], rep['skipped']), (0, 1))


class ArrangeChainIgnoresOpenCategory(unittest.TestCase):
    """開單分類（可以是「服務中」）跟排序要放進哪一串分類無關。"""
    def setUp(self):
        reset()
        self._old = bot.discord.CategoryChannel
        class Cat:
            def __init__(self, id, name, pos): self.id, self.name, self.position = id, name, pos
        bot.discord.CategoryChannel = Cat; self.Cat = Cat
    def tearDown(self): bot.discord.CategoryChannel = self._old
    def _guild(self, cats):
        by_id = {c.id: c for c in cats}
        return types.SimpleNamespace(id='g1', categories=cats, get_channel=lambda x: by_id.get(int(x)))
    def _t(self, cat): return types.SimpleNamespace(category=cat, category_id=cat.id)
    def test_open_in_service_sorting_still_finds_coin_chain_from_tickets(self):
        svc, a, a1 = self.Cat(1, '服務中', 0), self.Cat(2, '幣號工單', 1), self.Cat(3, '幣號工單1', 2)
        g = self._guild([svc, a, a1]); bot.setv('g1', 'ticket_category_id', 1)
        prefix, chain = bot.ticket_category_chain(g, [self._t(a), self._t(a1), self._t(a)], for_arrange=True)
        self.assertEqual(prefix, '幣號工單'); self.assertEqual([c.id for c in chain], [2, 3])
    def test_open_in_service_sorting_falls_back_to_default_name(self):
        svc, a = self.Cat(1, '服務中', 0), self.Cat(2, '幣號工單', 1)
        g = self._guild([svc, a]); bot.setv('g1', 'ticket_category_id', 1)
        prefix, chain = bot.ticket_category_chain(g, [self._t(svc)], for_arrange=True)      # 工單都在服務中，沒有其他線索
        self.assertEqual(prefix, '幣號工單'); self.assertEqual([c.id for c in chain], [2])
    def test_open_in_service_and_no_coin_categories_means_nothing_to_sort(self):
        svc = self.Cat(1, '服務中', 0); g = self._guild([svc]); bot.setv('g1', 'ticket_category_id', 1)
        self.assertEqual(bot.ticket_category_chain(g, [self._t(svc)], for_arrange=True), (None, []))
    def test_opening_tickets_still_uses_the_service_chain(self):
        svc, svc1, a = self.Cat(1, '服務中', 0), self.Cat(4, '服務中1', 1), self.Cat(2, '幣號工單', 2)
        g = self._guild([svc, svc1, a]); bot.setv('g1', 'ticket_category_id', 1)
        prefix, chain = bot.ticket_category_chain(g)        # 開單：新單照樣放進「服務中」那串
        self.assertEqual(prefix, '服務中'); self.assertEqual([c.id for c in chain], [1, 4])
    def test_no_open_category_and_no_hints_stays_as_before(self):
        a = self.Cat(2, '幣號工單', 1); g = self._guild([a])      # 沒設定開單分類、也沒有工單線索：不要自己猜
        self.assertEqual(bot.ticket_category_chain(g, [], for_arrange=True), (None, []))
    def test_normal_base_unchanged(self):
        a, a1 = self.Cat(2, '幣號工單', 1), self.Cat(3, '幣號工單1', 2); g = self._guild([a, a1]); bot.setv('g1', 'ticket_category_id', 2)
        self.assertEqual([c.id for c in bot.ticket_category_chain(g, for_arrange=True)[1]], [2, 3])

class PermissionChecks(unittest.TestCase):
    """客人不能變成管理員：@everyone 不算、一般客人不算；每個管理來源都說得出原因。"""
    def setUp(self):
        reset(); self._old = bot.discord.Member
        class FM:
            def __init__(self, roles=(), admin=False, manage_guild=False, uid=5):
                self.id = uid; self.mention = f'<@{uid}>'; self.guild = types.SimpleNamespace(id='g1')
                self.guild_permissions = types.SimpleNamespace(administrator=admin, manage_guild=manage_guild)
                self.roles = [types.SimpleNamespace(id='g1', name='@everyone')] + [types.SimpleNamespace(id=i, name=n) for i, n in roles]
        bot.discord.Member = FM; self.FM = FM
    def tearDown(self): bot.discord.Member = self._old
    def test_plain_customer_is_not_admin(self):
        c = self.FM(roles=[(10, '客人'), (11, 'VIP')])
        self.assertFalse(bot.admin(c)); self.assertFalse(bot.strict_admin(c)); self.assertEqual(bot.admin_reasons(c), [])
    def test_everyone_set_as_admin_or_support_role_is_ignored(self):
        bot.setv('g1', 'admin_role_id', 'g1'); bot.setv('g1', 'support_role_id', 'g1')
        c = self.FM(); self.assertFalse(bot.admin(c)); self.assertFalse(bot.strict_admin(c))
    def test_non_member_is_never_admin(self):
        self.assertFalse(bot.admin(object()))
    def test_each_admin_source_works_with_reason(self):
        bot.setv('g1', 'admin_role_id', '20'); bot.setv('g1', 'support_role_id', '30')
        cases = [(self.FM(admin=True), True, True), (self.FM(manage_guild=True), True, True), (self.FM(roles=[(20, '老闆')]), True, True),
                 (self.FM(roles=[(30, '客服')]), True, False), (self.FM(roles=[(40, '店長')]), True, True)]
        for m, a, s in cases:
            self.assertEqual((bot.admin(m), bot.strict_admin(m)), (a, s)); self.assertTrue(bot.admin_reasons(m))
    def test_report_explains_and_flags_bad_config(self):
        bot.setv('g1', 'admin_role_id', 'g1')
        guild = types.SimpleNamespace(id='g1', get_role=lambda x: None)
        txt = bot.permission_report(self.FM(roles=[(10, '客人')]), guild)
        self.assertIn('不是管理員', txt); self.assertIn('@everyone', txt)
    def test_customer_cannot_run_rename_command(self):
        import asyncio
        sent = []
        class R:
            async def send_message(self, msg='', **k): sent.append(msg)
            async def defer(self, **k): sent.append('DEFERRED')
        i = types.SimpleNamespace(user=self.FM(roles=[(10, '客人')]), response=R(), guild=types.SimpleNamespace(id='g1'), channel=None)
        asyncio.run(bot._rename_entry(i, '幣號', '待付款', '100M', 1))
        self.assertEqual(len(sent), 1); self.assertIn('沒有權限', sent[0])
    def test_set_admin_role_rejects_everyone(self):
        import asyncio
        sent = []
        class R:
            async def send_message(self, msg='', **k): sent.append(msg)
        i = types.SimpleNamespace(user=types.SimpleNamespace(guild_permissions=types.SimpleNamespace(administrator=True)), response=R(), guild=types.SimpleNamespace(id=777))
        asyncio.run(bot.set_admin_role(i, types.SimpleNamespace(id=777, mention='@everyone')))
        self.assertIn('@everyone', sent[0]); self.assertEqual(bot.get(777, 'admin_role_id', ''), '')

class CloseAllTickets(unittest.TestCase):
    """/關所有工單：依狀態找工單、確認後逐張關、一張失敗不影響其他張。"""
    def setUp(self):
        reset(); bot.q('DELETE FROM ticket_channels')
        self._old = (bot.discord.TextChannel, getattr(bot.discord, 'HTTPException', None))
        class FakeText:
            def __init__(self, cid, name, created=0): self.id = cid; self.name = name; self.created_at = created; self.mention = f'<#{cid}>'; self.sent = []
            async def send(self, text='', **k): self.sent.append(text)
        bot.discord.TextChannel = FakeText; self.FT = FakeText
        bot.discord.HTTPException = type('HTTPException', (Exception,), {})
    def tearDown(self): bot.discord.TextChannel, bot.discord.HTTPException = self._old
    def _order(self, cid, status, ticket='0001'):
        c = bot.db.cursor()
        c.execute('INSERT INTO orders(ticket_no,channel_id,guild_id,product,quantity,unit_price,total_price,status,buyer_id,created_at,service_type) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (ticket, str(cid), 'g1', '100M', 1, 100, 100, status, '111', bot.now(), '幣號'))
        bot.db.commit(); return c.lastrowid
    def _guild(self, chans):
        return types.SimpleNamespace(id='g1', text_channels=chans, get_channel=lambda x: next((c for c in chans if c.id == int(x)), None))
    def _tickets(self):
        a = self.FT(1, '待付款-幣號-0301-100M', 1); self._order(1, '待付款', '0301')
        b = self.FT(2, '待倒-代肝-0302-100M', 2); self._order(2, '待倒', '0302')
        c = self.FT(3, '待付款-幣號-0303-50M', 3)                     # 沒有訂單，狀態從名稱看
        d = self.FT(4, '結單-幣號-0304-100M', 4); self._order(4, '結單', '0304')
        e = self.FT(5, '一般聊天', 5)                                  # 不是工單
        f = self.FT(6, 'ticket-0305', 6)                               # 剛開、還沒下單
        return [a, b, c, d, e, f]
    # ── 找工單 ──
    def test_status_detection_order_first_then_name_then_no_order(self):
        a, b, c, d, e, f = self._tickets()
        self.assertEqual([bot.ticket_current_status(x) for x in (a, b, c, d, f)], ['待付款', '待倒', '待付款', '結單', '無訂單'])
    def test_filter_by_each_status(self):
        g = self._guild(self._tickets())
        ids = lambda st: [ch.id for ch, _ in bot.find_tickets_to_close(g, st)]
        self.assertEqual(ids('待付款'), [1, 3]); self.assertEqual(ids('待倒'), [2]); self.assertEqual(ids('無訂單'), [6]); self.assertEqual(ids('處理中'), [])
    def test_all_excludes_closed_and_non_tickets(self):
        g = self._guild(self._tickets())
        self.assertEqual([ch.id for ch, _ in bot.find_tickets_to_close(g, bot.CLOSE_ALL_ALL)], [1, 2, 3, 6])      # 結單(4)與一般頻道(5)不會被關
    def test_closed_status_can_never_be_selected_to_close(self):
        g = self._guild(self._tickets()); self.assertEqual(bot.find_tickets_to_close(g, '結單'), [])
    # ── 逐張關閉 ──
    def _interaction(self):
        edits = []
        async def edit(content='', **k): edits.append(content)
        i = types.SimpleNamespace(user=types.SimpleNamespace(id=1, mention='<@1>'), guild=None, channel=self.FT(99, 'cmd'), edit_original_response=edit)
        return i, edits
    def _run(self, chans, status, ids, closer):
        import asyncio
        i, edits = self._interaction(); i.guild = self._guild(chans)
        old = (bot.close_ticket_channel, bot.post_channel_log)
        async def fake_log(*a, **k): pass
        bot.close_ticket_channel, bot.post_channel_log = closer, fake_log
        try: res = asyncio.run(bot.run_close_all(i, status, ids))
        finally: bot.close_ticket_channel, bot.post_channel_log = old
        return res, edits
    def test_closes_only_selected_status_and_reports(self):
        chans = self._tickets(); closed = []
        async def closer(guild, ch, op, label, revoke): closed.append(ch.id); return {'ticket_number': str(ch.id), 'moved': True, 'rename_ok': True, 'old_status': 'x', 'perm_msg': 'ok'}
        res, edits = self._run(chans, '待付款', [1, 3], closer)
        self.assertEqual(closed, [1, 3]); self.assertEqual(res['closed'], 2); self.assertIn('已關閉 **2** 張', edits[-1])
    def test_status_changed_since_preview_is_skipped(self):
        chans = self._tickets(); closed = []
        async def closer(guild, ch, op, label, revoke): closed.append(ch.id); return {'ticket_number': '1', 'moved': True, 'rename_ok': True, 'old_status': 'x', 'perm_msg': 'ok'}
        bot.q("UPDATE orders SET status='處理中' WHERE channel_id='1'")        # 預覽之後，1 號被改成處理中
        res, _ = self._run(chans, '待付款', [1, 3], closer)
        self.assertEqual(closed, [3]); self.assertEqual(res['skipped'], 1)
    def test_one_failure_does_not_stop_the_rest(self):
        chans = self._tickets(); closed = []
        async def closer(guild, ch, op, label, revoke):
            if ch.id == 1: raise RuntimeError('boom')
            closed.append(ch.id); return {'ticket_number': str(ch.id), 'moved': False, 'rename_ok': False, 'old_status': 'x', 'perm_msg': 'ok'}
        res, edits = self._run(chans, bot.CLOSE_ALL_ALL, [1, 2, 3], closer)
        self.assertEqual(closed, [2, 3]); self.assertEqual(len(res['errors']), 1)
        self.assertIn('沒能移入結單分類', edits[-1]); self.assertIn('改名失敗', edits[-1]); self.assertIn('出錯', edits[-1])
    def test_cap_per_run_and_remaining_reported(self):
        old = bot.CLOSE_ALL_MAX_PER_RUN; bot.CLOSE_ALL_MAX_PER_RUN = 2
        try:
            chans = [self.FT(n, f'待付款-幣號-{n:04d}-100M', n) for n in range(10, 15)]
            for c in chans: self._order(c.id, '待付款', f'{c.id:04d}')
            closed = []
            async def closer(guild, ch, op, label, revoke): closed.append(ch.id); return {'ticket_number': str(ch.id), 'moved': True, 'rename_ok': True, 'old_status': 'x', 'perm_msg': 'ok'}
            res, edits = self._run(chans, '待付款', [c.id for c in chans], closer)
        finally: bot.CLOSE_ALL_MAX_PER_RUN = old
        self.assertEqual(closed, [10, 11]); self.assertEqual(res['rest'], 3); self.assertIn('還有 **3** 張', edits[-1])
    def test_running_flag_is_released_even_after_errors(self):
        chans = self._tickets()
        async def closer(*a, **k): raise RuntimeError('x')
        self._run(chans, '待付款', [1], closer); self.assertNotIn('g1', bot._CLOSE_ALL_RUNNING)
    # ── 確認按鈕 ──
    def test_only_the_operator_can_confirm(self):
        import asyncio
        v = bot.CloseAllConfirmView(1, '待付款', [1, 3]); sent = []
        class R:
            async def send_message(self, m='', **k): sent.append(m)
        other = types.SimpleNamespace(user=types.SimpleNamespace(id=2), response=R()); mine = types.SimpleNamespace(user=types.SimpleNamespace(id=1), response=R())
        self.assertFalse(asyncio.run(v.interaction_check(other))); self.assertTrue(sent)
        self.assertTrue(asyncio.run(v.interaction_check(mine)))
    # ── 單張關閉（/關閉工單 與批量共用）──
    def _close_env(self):
        calls = {}
        async def revoke(ch, buyer): return True, '已移除客人權限'
        async def safe_edit(ch, timeout=10, **kw): ch.name = kw.get('name', ch.name); return True
        async def move(ch, svc): calls['moved'] = ch.id; return True
        async def log(*a, **k): pass
        async def announce(*a, **k): calls['announced'] = True
        saved = {k: getattr(bot, k) for k in ('revoke_buyer_access', '_safe_edit_channel', 'move_closed_ticket_to_service_category', 'post_channel_log', 'status_announce', 'ClosedTicketView')}
        bot.revoke_buyer_access, bot._safe_edit_channel, bot.move_closed_ticket_to_service_category = revoke, safe_edit, move
        bot.post_channel_log, bot.status_announce, bot.ClosedTicketView = log, announce, (lambda: None)
        return calls, saved
    def test_close_ticket_channel_marks_order_closed_and_moves(self):
        import asyncio
        calls, saved = self._close_env()
        try:
            ch = self.FT(21, '待付款-幣號-0321-100M'); self._order(21, '待付款', '0321')
            res = asyncio.run(bot.close_ticket_channel(types.SimpleNamespace(id='g1'), ch, types.SimpleNamespace(id=1, mention='<@1>'), '關所有工單', True))
        finally:
            for k, v in saved.items(): setattr(bot, k, v)
        self.assertEqual(bot.q("SELECT status FROM orders WHERE channel_id='21'", (), True)[0]['status'], '結單')
        self.assertTrue(res['moved']); self.assertEqual(calls['moved'], 21); self.assertTrue(calls.get('announced')); self.assertTrue(ch.name.startswith('結單'))
        self.assertTrue(ch.sent and '工單已關閉' in ch.sent[0])
    def test_single_close_command_still_replies_the_same(self):
        import asyncio
        calls, saved = self._close_env(); sent = []
        class R:
            def is_done(self): return False
            async def defer(self, **k): pass
        class F:
            async def send(self, m='', **k): sent.append(m)
        old_admin = bot.admin; bot.admin = lambda m: True
        try:
            ch = self.FT(22, '待付款-幣號-0322-100M'); self._order(22, '待付款', '0322')
            i = types.SimpleNamespace(user=types.SimpleNamespace(id=1, mention='<@1>'), guild=types.SimpleNamespace(id='g1'), channel=ch, response=R(), followup=F())
            asyncio.run(bot.close_ticket_command(i))
        finally:
            bot.admin = old_admin
            for k, v in saved.items(): setattr(bot, k, v)
        self.assertEqual(len(sent), 1); self.assertIn('#0322 已關閉', sent[0]); self.assertIn('已移入對應結單分類', sent[0])


class PermissionGate(unittest.TestCase):
    """統一權限閘門：客人／客服／店長各自能用哪些指令；表裡的指令名稱必須真的存在；新指令沒列就預設只有管理員。"""
    def setUp(self):
        reset(); self._old = bot.discord.Member
        class FM:
            def __init__(self, roles=(), admin=False, manage_guild=False, uid=5):
                self.id = uid; self.mention = f'<@{uid}>'; self.guild = types.SimpleNamespace(id='g1')
                self.guild_permissions = types.SimpleNamespace(administrator=admin, manage_guild=manage_guild)
                self.roles = [types.SimpleNamespace(id='g1', name='@everyone')] + [types.SimpleNamespace(id=i, name=n) for i, n in roles]
        bot.discord.Member = FM; self.FM = FM
        bot.setv('g1', 'support_role_id', '30'); bot.setv('g1', 'admin_role_id', '')
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.py'), encoding='utf-8') as f: self.src = f.read()
    def tearDown(self): bot.discord.Member = self._old; bot.setv('g1', 'support_role_id', '')
    def test_levels(self):
        self.assertEqual(bot.command_level('查詢餘額'), 'public'); self.assertEqual(bot.command_level('增加餘額'), 'strict')
        self.assertEqual(bot.command_level('設定訊息'), 'admin'); self.assertEqual(bot.command_level('完全沒聽過的新指令'), 'admin')
        self.assertEqual(bot.command_level(None), 'admin'); self.assertEqual(bot.command_level(''), 'admin')
    def test_customer_gets_public_only(self):
        c = self.FM(roles=[(10, '客人')])
        self.assertTrue(bot.check_command_permission(c, '查詢餘額')[0]); self.assertTrue(bot.check_command_permission(c, '購買相關資訊')[0])
        for name in ('設定訊息', '修改狀態', '關閉工單', '增加餘額', '關所有工單', '關閉結單工單', None, '新指令'):
            ok, msg = bot.check_command_permission(c, name); self.assertFalse(ok, name); self.assertTrue(msg)
    def test_support_role_is_admin_but_not_strict(self):
        sup = self.FM(roles=[(30, '客服')])
        self.assertTrue(bot.check_command_permission(sup, '修改狀態')[0]); self.assertTrue(bot.check_command_permission(sup, '發號')[0])
        for name in sorted(bot.STRICT_COMMANDS):
            ok, msg = bot.check_command_permission(sup, name); self.assertFalse(ok, name); self.assertIn('店長等級', msg)
    def test_boss_can_use_everything(self):
        for boss in (self.FM(admin=True), self.FM(manage_guild=True), self.FM(roles=[(40, '店長')])):
            for name in sorted(bot.STRICT_COMMANDS) + ['修改狀態', '查詢餘額', None]: self.assertTrue(bot.check_command_permission(boss, name)[0], name)
    def test_non_member_and_dm_are_denied(self):
        ok, msg = bot.check_command_permission(object(), '查詢餘額'); self.assertFalse(ok); self.assertIn('伺服器', msg)
    def test_tree_gate_blocks_customer_and_lets_boss_through(self):
        import asyncio
        sent = []
        class R:
            def is_done(self): return False
            async def send_message(self, m='', **k): sent.append((m, k.get('ephemeral')))
        mk = lambda user, name: types.SimpleNamespace(user=user, data={'name': name}, response=R())
        gate = lambda user, name: asyncio.run(bot.CTCommandTree.interaction_check(None, mk(user, name)))
        customer = self.FM(roles=[(10, '客人')])
        self.assertFalse(gate(customer, '修改狀態')); self.assertEqual(len(sent), 1); self.assertTrue(sent[0][1]); self.assertIn('管理權限', sent[0][0])
        self.assertFalse(asyncio.run(bot.CTCommandTree.interaction_check(None, types.SimpleNamespace(user=customer, data=None, response=R()))))   # 名稱不明 → 預設拒絕
        self.assertEqual(len(sent), 2)
        self.assertTrue(gate(customer, '查詢餘額')); self.assertEqual(len(sent), 2)                  # 公開指令放行，不回拒絕訊息
        self.assertFalse(gate(self.FM(roles=[(30, '客服')]), '關閉結單工單')); self.assertIn('店長等級', sent[-1][0])
        self.assertTrue(gate(self.FM(admin=True), '關所有工單')); self.assertEqual(len(sent), 3)
        self.assertFalse(gate(object(), '查詢餘額')); self.assertIn('伺服器', sent[-1][0])           # 私訊／非成員
    def test_every_listed_command_really_exists(self):
        import re
        real = set(re.findall(r"\.command\(name='([^']+)'", self.src))
        for name in sorted(bot.STRICT_COMMANDS | bot.PUBLIC_COMMANDS): self.assertIn(name, real, f'權限表裡的「{name}」不是真的指令（打錯字？）')
    def test_strict_commands_also_check_inside_the_function(self):
        import ast, re
        tree = ast.parse(self.src); found = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.AsyncFunctionDef):
                for d in n.decorator_list:
                    m = re.search(r"\.command\(name='([^']+)'", ast.get_source_segment(self.src, d) or '')
                    if m: found[m.group(1)] = ast.get_source_segment(self.src, n)
        delegated = {'增加餘額', '扣除餘額', '設定管理員'}        # 增減餘額交給 balance_change（內有 strict_admin）；設定管理員要求 Discord 管理員
        for name in sorted(bot.STRICT_COMMANDS - delegated): self.assertIn('strict_admin(', found[name], name)
        self.assertIn('strict_admin(', self.src[self.src.index('async def balance_change'):self.src.index("@bot.tree.command(name='增加餘額'")])
        self.assertIn('administrator', found['設定管理員'])
    def test_deny_text_matches_level(self):
        import asyncio
        sent = []
        class R:
            def is_done(self): return False
            async def send_message(self, m='', **k): sent.append(m)
        i = types.SimpleNamespace(response=R())
        asyncio.run(bot.deny(i)); asyncio.run(bot.deny(i, True))
        self.assertIn('管理權限', sent[0]); self.assertNotIn('管理頻道', sent[0]); self.assertIn('店長等級', sent[1])

class CloseSafety(unittest.TestCase):
    """關單的安全網：一般頻道／機器人用的頻道不能被當工單關；客人不能自己關進行中的訂單；餘額退款要店長等級。"""
    def setUp(self):
        reset(); bot.q('DELETE FROM ticket_channels')
        self._old = (bot.discord.TextChannel, getattr(bot.discord, 'HTTPException', None))
        class FakeText:
            def __init__(self, cid, name, created=0, category_id=None, overwrites=None):
                self.id = cid; self.name = name; self.created_at = created; self.mention = f'<#{cid}>'; self.sent = []
                self.category_id = category_id; self.overwrites = overwrites or {}
            async def send(self, text='', **k): self.sent.append(text)
        bot.discord.TextChannel = FakeText; self.FT = FakeText
        bot.discord.HTTPException = type('HTTPException', (Exception,), {})
        self.guild = types.SimpleNamespace(id='g1')
        for k in bot.PROTECTED_CHANNEL_SETTINGS: bot.setv('g1', k, '')
    def tearDown(self):
        bot.discord.TextChannel, bot.discord.HTTPException = self._old
        for k in bot.PROTECTED_CHANNEL_SETTINGS: bot.setv('g1', k, '')
    def _order(self, cid, status, ticket='0001', buyer='111', completed=None):
        c = bot.db.cursor()
        c.execute('INSERT INTO orders(ticket_no,channel_id,guild_id,product,quantity,unit_price,total_price,status,buyer_id,created_at,service_type,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  (ticket, str(cid), 'g1', '100M', 1, 100, 100, status, buyer, bot.now(), '幣號', completed))
        bot.db.commit(); return c.lastrowid
    def test_normal_channel_is_not_closable(self):
        self.assertIn('不是工單', bot.closable_ticket_problem(self.guild, self.FT(1, '一般聊天')))
        self.assertIn('不是工單', bot.closable_ticket_problem(self.guild, self.FT(2, 'general')))
    def test_real_ticket_is_closable(self):
        self._order(3, '待付款', '0301'); self.assertIsNone(bot.closable_ticket_problem(self.guild, self.FT(3, '待付款-幣號-0301-100M')))
        self.assertIsNone(bot.closable_ticket_problem(self.guild, self.FT(4, 'ticket-0305')))
    def test_bot_channels_are_protected_even_if_they_have_orders_or_ticket_like_names(self):
        ch = self.FT(5, '工單狀態-0001'); self._order(5, '待付款', '0001')
        self.assertIsNone(bot.closable_ticket_problem(self.guild, ch))
        bot.setv('g1', 'status_channel_id', '5'); self.assertIn('機器人使用中', bot.closable_ticket_problem(self.guild, ch))
        for key in bot.PROTECTED_CHANNEL_SETTINGS:
            bot.setv('g1', key, '77'); self.assertTrue(bot.is_protected_channel(self.guild, self.FT(77, 'x')), key); bot.setv('g1', key, '')
    def test_close_refuses_non_ticket_and_touches_nothing(self):
        import asyncio
        ch = self.FT(6, '一般聊天'); called = []
        old = (bot._safe_edit_channel, bot.revoke_buyer_access, bot.move_closed_ticket_to_service_category)
        async def boom(*a, **k): called.append(1)
        bot._safe_edit_channel = bot.revoke_buyer_access = bot.move_closed_ticket_to_service_category = boom
        try:
            with self.assertRaises(ValueError): asyncio.run(bot.close_ticket_channel(self.guild, ch, types.SimpleNamespace(id=1, mention='<@1>')))
        finally: bot._safe_edit_channel, bot.revoke_buyer_access, bot.move_closed_ticket_to_service_category = old
        self.assertEqual(called, []); self.assertEqual(ch.sent, [])
    def test_closing_an_already_done_order_keeps_original_completed_time(self):
        import asyncio
        oid = self._order(7, '結單', '0307', completed='2026-01-01 10:00:00'); ch = self.FT(7, '結單-幣號-0307-100M')
        saved = {k: getattr(bot, k) for k in ('revoke_buyer_access', '_safe_edit_channel', 'move_closed_ticket_to_service_category', 'post_channel_log', 'status_announce')}
        async def rv(c, b=None): return True, '已移除'
        async def se(*a, **k): return True
        async def mv(*a, **k): return True
        async def nop(*a, **k): return None
        bot.revoke_buyer_access, bot._safe_edit_channel, bot.move_closed_ticket_to_service_category, bot.post_channel_log, bot.status_announce = rv, se, mv, nop, nop
        try: res = asyncio.run(bot.close_ticket_channel(self.guild, ch, types.SimpleNamespace(id=1, mention='<@1>'), '關閉結單工單'))
        finally:
            for k, v in saved.items(): setattr(bot, k, v)
        self.assertEqual(bot.q('SELECT completed_at FROM orders WHERE id=?', (oid,), True)[0]['completed_at'], '2026-01-01 10:00:00')
        self.assertEqual(res['old_status'], '結單'); self.assertTrue(res['moved'])
    def test_closing_an_open_order_sets_done_and_time(self):
        import asyncio
        oid = self._order(8, '待付款', '0308'); ch = self.FT(8, '待付款-幣號-0308-100M')
        saved = {k: getattr(bot, k) for k in ('revoke_buyer_access', '_safe_edit_channel', 'move_closed_ticket_to_service_category', 'post_channel_log', 'status_announce')}
        async def rv(c, b=None): return True, 'ok'
        async def se(*a, **k): return True
        async def nop(*a, **k): return None
        bot.revoke_buyer_access, bot._safe_edit_channel, bot.move_closed_ticket_to_service_category, bot.post_channel_log, bot.status_announce = rv, se, se, nop, nop
        try: asyncio.run(bot.close_ticket_channel(self.guild, ch, types.SimpleNamespace(id=1, mention='<@1>')))
        finally:
            for k, v in saved.items(): setattr(bot, k, v)
        r = bot.q('SELECT status,completed_at FROM orders WHERE id=?', (oid,), True)[0]; self.assertEqual(r['status'], '結單'); self.assertTrue(r['completed_at'])
    def test_buyer_can_only_close_unpaid_or_delivered(self):
        for st, ok in (('待付款', True), ('待洽談', True), ('待處理', True), ('已取消', True), ('待收貨', True), ('結單', True),
                       ('待確認付款', False), ('待排單', False), ('待交貨', False), ('處理中', False), ('有爭議', False), ('待倒', False)):
            cid = 100 + len(st) * 10 + (1 if ok else 0) + hash(st) % 7
            bot.q('DELETE FROM orders WHERE channel_id=?', (str(cid),)); self._order(cid, st, '0400')
            ch = self.FT(cid, f'{st}-幣號-0400-100M'); res = bot.buyer_close_problem(ch)
            self.assertEqual(res is None, ok, st)
            if not ok: self.assertIn(st, res)
        self.assertIsNone(bot.buyer_close_problem(self.FT(999, 'ticket-0401')))          # 還沒下單
    def test_balance_refund_pending(self):
        row = lambda **k: {'balance_paid': 0, 'balance_refunded': 0, **k}
        self.assertTrue(bot.balance_refund_pending(row(balance_paid=500), '待交貨'))
        self.assertTrue(bot.balance_refund_pending(row(balance_paid=500), '處理中'))
        self.assertFalse(bot.balance_refund_pending(row(balance_paid=500, balance_refunded=1), '待交貨'))
        self.assertFalse(bot.balance_refund_pending(row(balance_paid=500), '待收貨')); self.assertFalse(bot.balance_refund_pending(row(balance_paid=500), '結單'))
        self.assertFalse(bot.balance_refund_pending(row(), '待交貨')); self.assertFalse(bot.balance_refund_pending({}, '待交貨'))
    def test_support_cannot_cancel_balance_paid_order(self):
        import asyncio
        oid = self._order(9, '待交貨', '0309'); bot.q('UPDATE orders SET balance_paid=500,balance_refunded=0 WHERE id=?', (oid,))
        sent = []
        class F:
            async def send(self, m='', **k): sent.append(m)
        class R:
            async def defer(self, **k): pass
        old = (bot.strict_admin, bot.admin)
        bot.strict_admin = lambda m: False; bot.admin = lambda m: True      # 客服：是管理員，但不是店長等級
        try:
            i = types.SimpleNamespace(user=types.SimpleNamespace(id=1, mention='<@1>'), response=R(), followup=F(), guild=types.SimpleNamespace(id='g1'))
            asyncio.run(bot.change_status(i, self.FT(9, '待交貨-幣號-0309-100M'), '已取消'))
        finally: bot.strict_admin, bot.admin = old
        self.assertEqual(len(sent), 1); self.assertIn('店長等級', sent[0])
        r = bot.q('SELECT status,balance_refunded FROM orders WHERE id=?', (oid,), True)[0]; self.assertEqual((r['status'], r['balance_refunded']), ('待交貨', 0))

class CloseDoneTickets(unittest.TestCase):
    """/關閉結單工單：只找「已結單、但客人還看得到或沒進結單分類」的工單；確認後逐張關，已經關好的不動。"""
    def setUp(self):
        reset(); bot.q('DELETE FROM ticket_channels')
        self._old = (bot.discord.TextChannel, getattr(bot.discord, 'HTTPException', None))
        class FakeText:
            def __init__(self, cid, name, created=0, category_id=None, overwrites=None):
                self.id = cid; self.name = name; self.created_at = created; self.mention = f'<#{cid}>'; self.sent = []
                self.category_id = category_id; self.overwrites = overwrites or {}
            async def send(self, text='', **k): self.sent.append(text)
        class Target:
            def __init__(self, i): self.id = i
        bot.discord.TextChannel = FakeText; self.FT = FakeText; self.Target = Target
        self._old_cust = bot._member_is_customer_like; bot._CLOSE_STUCK.clear()
        async def _cust(ch, target): return True       # 測試用假頻道沒有 guild 成員資料，一律當客人
        bot._member_is_customer_like = _cust
        bot.discord.HTTPException = type('HTTPException', (Exception,), {})
        bot.setv('g1', 'service_category_coin_id', '900'); bot.setv('g1', 'service_category_boost_id', '901')
        for k in bot.PROTECTED_CHANNEL_SETTINGS: bot.setv('g1', k, '')
    def tearDown(self):
        bot._member_is_customer_like = self._old_cust
        bot.discord.TextChannel, bot.discord.HTTPException = self._old
        bot.setv('g1', 'service_category_coin_id', ''); bot.setv('g1', 'service_category_boost_id', '')
        for k in bot.PROTECTED_CHANNEL_SETTINGS: bot.setv('g1', k, '')
    def _order(self, cid, status, ticket, service='幣號', buyer='111'):
        c = bot.db.cursor()
        c.execute('INSERT INTO orders(ticket_no,channel_id,guild_id,product,quantity,unit_price,total_price,status,buyer_id,created_at,service_type) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (ticket, str(cid), 'g1', '100M', 1, 100, 100, status, buyer, bot.now(), service))
        bot.db.commit()
    def _guild(self, chans): return types.SimpleNamespace(id='g1', text_channels=chans, get_channel=lambda x: next((c for c in chans if c.id == int(x)), None))
    def _chans(self):
        see = lambda: {self.Target(111): types.SimpleNamespace(view_channel=True)}
        hidden = lambda: {self.Target(111): types.SimpleNamespace(view_channel=False)}
        a = self.FT(1, '結單-幣號-0501-100M', 1, category_id=5, overwrites=see()); self._order(1, '結單', '0501')           # 客人看得到＋不在結單分類
        b = self.FT(2, '結單-幣號-0502-100M', 2, category_id=900, overwrites=hidden()); self._order(2, '結單', '0502')    # 已經關好
        c = self.FT(3, '待付款-幣號-0503-100M', 3, category_id=5, overwrites=see()); self._order(3, '待付款', '0503')     # 還沒結單
        d = self.FT(4, '結單-幣號-0504-100M', 4, category_id=900, overwrites=see()); self._order(4, '結單', '0504')       # 在結單分類，但客人還看得到
        e = self.FT(5, '結單-幣號-0505-100M', 5, category_id=5, overwrites=hidden()); self._order(5, '結單', '0505')     # 客人看不到，但沒進結單分類
        f = self.FT(6, '結單-代肝-0506-100M', 6, category_id=901, overwrites=hidden()); self._order(6, '結單', '0506', service='代肝')  # 代肝已關好
        g = self.FT(7, '一般聊天', 7, category_id=5)
        return [a, b, c, d, e, f, g]
    def test_finds_only_done_tickets_not_really_closed(self):
        found = _run_async(bot.find_done_tickets_to_close(self._guild(self._chans())))
        self.assertEqual([ch.id for ch, _ in found], [1, 4, 5])
        why = {ch.id: w for ch, w in found}
        self.assertIn('客人仍看得到', why[1]); self.assertIn('不在結單分類', why[1]); self.assertEqual(why[4], '客人仍看得到'); self.assertEqual(why[5], '不在結單分類')
    def test_without_closed_category_only_customer_access_counts(self):
        bot.setv('g1', 'service_category_coin_id', '')
        found = _run_async(bot.find_done_tickets_to_close(self._guild(self._chans())))
        self.assertEqual([ch.id for ch, _ in found], [1, 4])
    def test_protected_channel_is_never_listed(self):
        bot.setv('g1', 'status_channel_id', '1')
        self.assertEqual([ch.id for ch, _ in _run_async(bot.find_done_tickets_to_close(self._guild(self._chans())))], [4, 5])
    def test_nothing_to_do_when_all_closed(self):
        chans = self._chans()[1:2] + self._chans()[5:6]
        self.assertEqual(_run_async(bot.find_done_tickets_to_close(self._guild([c for c in chans if c.id in (2, 6)]))), [])
    def _run(self, chans, ids, closer):
        import asyncio
        edits = []
        async def edit(content='', **k): edits.append(content)
        i = types.SimpleNamespace(user=types.SimpleNamespace(id=1, mention='<@1>'), guild=self._guild(chans), channel=self.FT(99, 'cmd'), edit_original_response=edit)
        old = (bot.close_ticket_channel, bot.post_channel_log)
        async def fake_log(*a, **k): pass
        bot.close_ticket_channel, bot.post_channel_log = closer, fake_log
        try: res = asyncio.run(bot.run_close_all(i, bot.CLOSE_DONE_STATUS, ids, 'done'))
        finally: bot.close_ticket_channel, bot.post_channel_log = old
        return res, edits
    def test_run_closes_selected_and_skips_already_closed_or_reopened(self):
        chans = self._chans(); closed = []; labels = []
        async def closer(guild, ch, op, label, revoke, keep_order=False): closed.append(ch.id); labels.append(label); return {'ticket_number': str(ch.id), 'moved': True, 'rename_ok': True, 'old_status': '結單', 'perm_msg': 'ok'}
        bot.q("UPDATE orders SET status='處理中' WHERE channel_id='4'")                 # 只看名稱：訂單狀態是什麼都不影響
        chans[1].overwrites = {}                                                          # 2 號本來就關好了（預覽時被誤列的情況）
        chans[3].name = '處理中-幣號-0504-100M'                                           # 4 號預覽後被改名成非結單 → 略過
        res, edits = self._run(chans, [1, 2, 4, 5], closer)
        self.assertEqual(closed, [1, 5]); self.assertEqual(res['closed'], 2); self.assertEqual(res['skipped'], 2)
        self.assertEqual(set(labels), {'關閉結單工單'}); self.assertIn('關閉結單工單完成', edits[-1]); self.assertIn('已關閉 **2** 張', edits[-1])
    def test_detection_is_name_only_ignores_order_status(self):
        chans = self._chans()
        bot.q("UPDATE orders SET status='處理中' WHERE channel_id='1'")        # 名稱是結單、訂單卻不是 → 仍要抓
        bot.q("UPDATE orders SET status='結單' WHERE channel_id='3'")          # 名稱是待付款、訂單是結單 → 不抓
        found = _run_async(bot.find_done_tickets_to_close(self._guild(chans)))
        self.assertIn(1, [ch.id for ch, _ in found]); self.assertNotIn(3, [ch.id for ch, _ in found])
    def test_run_done_mode_never_touches_open_tickets(self):
        chans = self._chans(); closed = []
        async def closer(guild, ch, op, label, revoke, keep_order=False): closed.append(ch.id); return {'ticket_number': '1', 'moved': True, 'rename_ok': True, 'old_status': 'x', 'perm_msg': 'ok'}
        res, _ = self._run(chans, [3, 7], closer)                                          # 3＝待付款、7＝一般聊天
        self.assertEqual(closed, []); self.assertEqual(res['skipped'], 2)
    def test_confirm_view_carries_mode_and_needs_boss(self):
        v = bot.CloseAllConfirmView(1, bot.CLOSE_DONE_STATUS, [1, 4], mode='done'); self.assertEqual(v.mode, 'done')
        self.assertEqual(bot.CloseAllConfirmView(1, '待付款', [1]).mode, 'normal')
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.py'), encoding='utf-8') as f: src = f.read()
        self.assertIn('strict_admin(', src.split('async def on_yes(self, i):')[1][:120])
    def test_close_all_never_lists_bot_channels(self):
        bot.setv('g1', 'action_log_channel_id', '1')
        ids = [ch.id for ch, _ in bot.find_tickets_to_close(self._guild(self._chans()), bot.CLOSE_ALL_ALL)]
        self.assertNotIn(1, ids); self.assertIn(3, ids)

if __name__ == '__main__': unittest.main(verbosity=2)
