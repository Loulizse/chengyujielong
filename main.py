import asyncio
import json
import os
import random
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Set

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register

# 尝试导入拼音库
try:
    from pypinyin import Style, lazy_pinyin

    HAS_PYPINYIN = True
except ImportError:
    HAS_PYPINYIN = False
    logger.warning("pypinyin未安装，拼音匹配功能将不可用")


# ==================== 数据类定义 ====================
class Player:
    def __init__(self, user_id: str, user_name: str):
        self.user_id = user_id
        self.user_name = user_name


class IdiomRoom:
    """成语接龙游戏房间（单游戏模式）"""

    def __init__(self, group_id: str, owner_id: str, owner_name: str):
        self.group_id = group_id  # 群ID
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.players: Dict[str, Player] = {}  # 参与者 user_id -> Player
        self.status = "playing"  # 只有 playing 状态
        self.current_idiom: str = ""
        self.history: List[str] = []
        self.round: int = 1
        self.scores: Dict[str, int] = {}  # user_id -> 本轮得分
        self.start_time: str = datetime.now().isoformat()
        self.unified_msg_origin = None

        # 待定队列：存放无效接龙词语
        self.pending_queue: List[dict] = (
            []
        )  # 每个元素: {"word": str, "user_id": str, "user_name": str}

        # 计时器
        self.timer_task: Optional[asyncio.Task] = None
        self.timer_cancel_event: Optional[asyncio.Event] = None


# ==================== 主插件类 ====================
@register("idiom_jielong", "YourName", "成语接龙游戏插件（抢答版·单游戏模式）", "2.0.0")
class IdiomJielongPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        if not HAS_PYPINYIN:
            logger.error("请安装 pypinyin 库：pip install pypinyin")
        self.delay_min = 1.0  # 最小延迟秒数
        self.delay_max = 3.0  # 最大延迟秒数
        # 全局配置
        self.global_timeout_seconds = 180

        # 按群存储当前活动游戏（每个群最多一个）
        self.active_games: Dict[str, IdiomRoom] = {}

        # 数据存储路径
        self.curr_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_file = os.path.join(self.curr_dir, "idiom_scores.db")
        self.idiom_file = os.path.join(self.curr_dir, "idiom.json")

        # 初始化数据库
        self.conn = sqlite3.connect(self.db_file)
        self.cursor = self.conn.cursor()
        self.init_database()

        # 加载成语词库
        self.idioms: Set[str] = set()
        self.idiom_pinyin_map: Dict[str, tuple] = {}
        self.load_idioms()

        # 加载用户积分
        self.player_scores = self.load_scores()

    async def send_delayed(
        self, event: AstrMessageEvent, text: str, min_delay=1.0, max_delay=3.0
    ):
        """延迟随机秒数后发送消息"""
        delay = random.uniform(min_delay, max_delay)
        await asyncio.sleep(delay)
        chain = MessageChain([Plain(text)])
        # 使用统一消息源，确保能发到正确的群/私聊
        await self.context.send_message(event.unified_msg_origin, chain)

    # ---------- 数据库操作 ----------
    def init_database(self):
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS idiom_scores (
                user_id TEXT,
                user_name TEXT,
                session_id TEXT,
                score INTEGER,
                timestamp TEXT,
                PRIMARY KEY (user_id, session_id, timestamp)
            )
        """
        )
        self.cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS idiom_game_history (
                session_id TEXT,
                start_time TEXT,
                end_time TEXT,
                history TEXT,
                total_rounds INTEGER,
                participants INTEGER,
                scores TEXT,
                PRIMARY KEY (session_id, start_time)
            )
        """
        )
        self.conn.commit()

    def load_scores(self) -> Dict:
        scores = {}
        try:
            self.cursor.execute(
                "SELECT user_id, user_name, SUM(score) as total FROM idiom_scores GROUP BY user_id"
            )
            for user_id, user_name, total in self.cursor.fetchall():
                scores[user_id] = {"name": user_name, "score": total}
        except Exception as e:
            logger.error(f"加载积分失败: {e}")
        return scores

    def save_score(self, user_id: str, user_name: str, session_id: str, score: int):
        try:
            self.cursor.execute(
                "INSERT INTO idiom_scores (user_id, user_name, session_id, score, timestamp) VALUES (?, ?, ?, ?, ?)",
                (user_id, user_name, session_id, score, datetime.now().isoformat()),
            )
            self.conn.commit()
            if user_id not in self.player_scores:
                self.player_scores[user_id] = {"name": user_name, "score": 0}
            self.player_scores[user_id]["score"] += score
            self.player_scores[user_id]["name"] = user_name
        except Exception as e:
            logger.error(f"保存积分失败: {e}")

    def save_game_history(self, room: IdiomRoom):
        try:
            scores_json = json.dumps(room.scores, ensure_ascii=False)
            history_json = json.dumps(room.history, ensure_ascii=False)
            self.cursor.execute(
                "INSERT INTO idiom_game_history (session_id, start_time, end_time, history, total_rounds, participants, scores) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    room.group_id,
                    room.start_time,
                    datetime.now().isoformat(),
                    history_json,
                    len(room.history),
                    len(room.players),
                    scores_json,
                ),
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"保存游戏历史失败: {e}")

    # ---------- 拼音处理 ----------
    def remove_tone(self, pinyin_syllable: str) -> str:
        TONE_MAP = {
            "ā": "a",
            "á": "a",
            "ǎ": "a",
            "à": "a",
            "ē": "e",
            "é": "e",
            "ě": "e",
            "è": "e",
            "ī": "i",
            "í": "i",
            "ǐ": "i",
            "ì": "i",
            "ō": "o",
            "ó": "o",
            "ǒ": "o",
            "ò": "o",
            "ū": "u",
            "ú": "u",
            "ǔ": "u",
            "ù": "u",
            "ǖ": "ü",
            "ǘ": "ü",
            "ǚ": "ü",
            "ǜ": "ü",
            "ê": "e",
            "ń": "n",
            "ň": "n",
            "ǹ": "n",
            "ḿ": "m",
        }
        return "".join(TONE_MAP.get(ch, ch) for ch in pinyin_syllable)

    def get_word_pinyin_flexible(self, word: str, position: str = "first") -> str:
        if not word or len(word) != 4:
            return ""
        if word in self.idiom_pinyin_map:
            first, last = self.idiom_pinyin_map[word]
            return first if position == "first" else last
        if HAS_PYPINYIN:
            char = word[0] if position == "first" else word[-1]
            pinyin_list = lazy_pinyin(char, style=Style.NORMAL, errors="ignore")
            if pinyin_list:
                return pinyin_list[0].lower()
            return char.lower()
        return word[0] if position == "first" else word[-1]

    def can_chain(self, prev: str, curr: str) -> bool:
        if not prev or not curr:
            return False
        # 相同汉字直接成功
        if prev[-1] == curr[0]:
            logger.info(f"相同汉字接龙: {prev[-1]} == {curr[0]}")
            return True
        prev_last = self.get_word_pinyin_flexible(prev, "last")
        curr_first = self.get_word_pinyin_flexible(curr, "first")
        logger.info(f"拼音匹配: {prev} 尾音={prev_last}, {curr} 首音={curr_first}")
        return prev_last == curr_first

    def is_valid_four_chars(self, text: str) -> bool:
        if not text or len(text) != 4:
            return False
        return bool(re.match(r"^[\u4e00-\u9fff]+$", text))

    # ---------- 词库加载 ----------
    def load_idioms(self):
        self.idioms = set()
        self.idiom_pinyin_map = {}
        if not os.path.exists(self.idiom_file):
            logger.warning(f"成语词库文件不存在: {self.idiom_file}，使用默认词库")
            self.load_default_idioms()
            return
        try:
            with open(self.idiom_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    word = None
                    pinyin_full = None
                    if isinstance(item, str):
                        word = item
                    elif isinstance(item, dict) and "word" in item:
                        word = item["word"]
                        pinyin_full = item.get("pinyin", "")
                    if (
                        not word
                        or len(word) != 4
                        or not re.match(r"^[\u4e00-\u9fff]+$", word)
                    ):
                        continue
                    self.idioms.add(word)
                    if pinyin_full and isinstance(pinyin_full, str):
                        parts = pinyin_full.split()
                        if len(parts) == 4:
                            first = self.remove_tone(parts[0]).lower()
                            last = self.remove_tone(parts[-1]).lower()
                            self.idiom_pinyin_map[word] = (first, last)
            else:
                self.load_default_idioms()
                return
            logger.info(
                f"✅ 加载成语词库完成，共 {len(self.idioms)} 个成语，其中 {len(self.idiom_pinyin_map)} 个有拼音映射"
            )
        except Exception as e:
            logger.error(f"加载成语词库失败: {e}")
            self.load_default_idioms()

    def load_default_idioms(self):
        default_idioms = [
            "一马当先",
            "先见之明",
            "明察秋毫",
            "毫不介意",
            "意味深长",
            "长驱直入",
            "入木三分",
            "分秒必争",
            "争分夺秒",
            "妙笔生花",
            "花好月圆",
            "圆木警枕",
            "枕戈待旦",
            "旦夕之间",
            "间不容发",
            "发奋图强",
            "强人所难",
            "难能可贵",
            "贵耳贱目",
            "目不转睛",
        ]
        self.idioms = set(default_idioms)
        logger.info(f"✅ 加载默认成语词库完成，共 {len(self.idioms)} 个成语")

    # ---------- 游戏核心逻辑 ----------
    async def is_admin(self, user_id: str) -> bool:
        """检查是否为机器人管理员（可配置）"""
        # 你可以修改这里的管理员QQ号列表
        admin_list = ["706773532"]  # 替换为实际管理员QQ
        return user_id in admin_list

    async def send_room_message(self, room: IdiomRoom, message: str):
        if room.unified_msg_origin:
            delay = random.uniform(self.delay_min, self.delay_max)
            await asyncio.sleep(delay)
            chain = MessageChain([Plain(message)])
            await self.context.send_message(room.unified_msg_origin, chain)

    async def cancel_timer(self, room: IdiomRoom):
        if room.timer_task and not room.timer_task.done():
            if room.timer_cancel_event:
                room.timer_cancel_event.set()
            room.timer_task.cancel()
            try:
                await room.timer_task
            except asyncio.CancelledError:
                pass
        room.timer_task = None
        room.timer_cancel_event = None

    async def start_round_timer(self, room: IdiomRoom):
        await self.cancel_timer(room)
        room.timer_cancel_event = asyncio.Event()

        async def timer_task():
            try:
                await asyncio.wait_for(
                    room.timer_cancel_event.wait(), timeout=self.global_timeout_seconds
                )
                return
            except asyncio.TimeoutError:
                pass
            await self.send_room_message(
                room, f"⏰ {self.global_timeout_seconds}秒无人接龙，游戏自动结束！"
            )
            if room.scores:
                score_lines = ["📊 最终得分："]
                sorted_scores = sorted(
                    room.scores.items(), key=lambda x: x[1], reverse=True
                )
                for i, (uid, score) in enumerate(sorted_scores, 1):
                    player = room.players.get(uid)
                    name = player.user_name if player else f"玩家{uid}"
                    score_lines.append(f"{i}. {name}: {score}分")
                await self.send_room_message(room, "\n".join(score_lines))
                for uid, score in room.scores.items():
                    player = room.players.get(uid)
                    if player:
                        self.save_score(uid, player.user_name, room.group_id, score)
            self.save_game_history(room)
            # 结束游戏，清理
            if room.group_id in self.active_games:
                del self.active_games[room.group_id]
            await self.cancel_timer(room)

        room.timer_task = asyncio.create_task(timer_task())

    async def end_game_cleanup(self, room: IdiomRoom):
        """房主主动结束游戏时调用"""
        await self.cancel_timer(room)
        if room.group_id in self.active_games:
            del self.active_games[room.group_id]

    async def show_final_scores(self, room: IdiomRoom):
        if not room.scores:
            await self.send_room_message(room, "📊 本轮无人得分")
            return
        score_lines = ["📊 本轮得分情况："]
        sorted_scores = sorted(room.scores.items(), key=lambda x: x[1], reverse=True)
        for i, (uid, score) in enumerate(sorted_scores, 1):
            player = room.players.get(uid)
            name = player.user_name if player else f"玩家{uid}"
            score_lines.append(f"{i}. {name}: {score}分")
        await self.send_room_message(room, "\n".join(score_lines))
        for uid, score in room.scores.items():
            player = room.players.get(uid)
            if player:
                self.save_score(uid, player.user_name, room.group_id, score)

    # ---------- 接龙处理 ----------

    async def process_idiom(self, event: AstrMessageEvent, content: str, group_id: str):
        """处理接龙，不在库中的词语进入待定队列，有效接龙清空队列并加分"""
        room = self.active_games.get(group_id)
        if not room or room.status != "playing":
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        # 1. 格式检查
        if not self.is_valid_four_chars(content):
            await self.send_delayed(event, f"❌ {user_name}：'{content}' 不是四字词语")
            return

        # 2. 接龙规则检查
        if not self.can_chain(room.current_idiom, content):
            last_pinyin = self.get_word_pinyin_flexible(room.current_idiom, "last")
            await self.send_delayed(
                event,
                f"❌ {user_name}：'{content}' 无法接龙\n"
                f"📝 上一个词语：{room.current_idiom}\n"
                f"🎯 需要以拼音 '{last_pinyin}' 开头的词语",
            )
            return

        # 3. 重复检查
        if content in room.history:
            await self.send_delayed(
                event, f"❌ {user_name}：'{content}' 已经被使用过了"
            )
            return

        # 4. 判断是否在成语库中
        in_library = content in self.idioms

        if in_library:
            # 有效接龙：清空待定队列，给当前玩家加分，更新游戏状态

            score_gain = 5
            await self.cancel_timer(room)
            room.current_idiom = content
            room.history.append(content)
            room.scores[user_id] = room.scores.get(user_id, 0) + score_gain
            room.round += 1

            if user_id not in room.players:
                room.players[user_id] = Player(user_id, user_name)
                await self.send_room_message(
                    room, f"🎉 新玩家 {user_name} 加入游戏并抢答成功！"
                )

            await self.send_delayed(
                event,
                f"🎉 {user_name} 接龙成功！+{score_gain}分 (📚 成语库)\n"
                f"📝 当前词语：{content}\n"
                f"🏆 第{room.round}轮待接：{content}\n"
                f"📊 当前积分：{room.scores[user_id]}分",
            )
            await self.start_round_timer(room)
        else:
            # 不在库中：加入待定队列，不加分，不改变游戏状态
            room.pending_queue.append(
                {"word": content, "user_id": user_id, "user_name": user_name}
            )
            await self.send_delayed(
                event,
                f"⚠️ {user_name}：'{content}' 不在成语库中，已加入待定队列（第{len(room.pending_queue)}个）。\n"
                f"💡 需要由管理员使用 /c p 判定，或等待有效接龙清空队列。",
            )

    async def manage_pending(self, event: AstrMessageEvent, group_id: str, arg: str):
        """管理员查看或手动认可待定队列中的词语，认可后更新游戏状态"""
        room = self.active_games.get(group_id)
        if not room:
            await self.send_delayed(event, "当前没有进行中的游戏")
            return

        user_id = event.get_sender_id()
        # 检查权限：房主或管理员
        if not (user_id == room.owner_id or await self.is_admin(user_id)):
            await self.send_delayed(event, "只有房主或管理员可以管理待定队列")
            return

        if not arg:
            # 列出待定队列
            if not room.pending_queue:
                await self.send_delayed(event, "当前没有待定词语")
                return
            lines = ["📋 待定词语列表（需管理员判定）:"]
            for idx, item in enumerate(room.pending_queue, 1):
                lines.append(f"{idx}. {item['word']} - 来自 {item['user_name']}")
            await self.send_delayed(event, "\n".join(lines))
            return

        # 尝试认可指定序号的词语
        try:
            idx = int(arg) - 1
            if idx < 0 or idx >= len(room.pending_queue):
                await self.send_delayed(
                    event, f"序号无效，有效范围 1-{len(room.pending_queue)}"
                )
                return
            item = room.pending_queue.pop(idx)
            word = item["word"]
            target_user_id = item["user_id"]
            target_user_name = item["user_name"]

            # 1. 给该用户加5分
            room.scores[target_user_id] = room.scores.get(target_user_id, 0) + 5
            if target_user_id not in room.players:
                room.players[target_user_id] = Player(target_user_id, target_user_name)

            # 2. 将该词语设为当前接龙词语，并更新游戏状态
            await self.cancel_timer(room)  # 取消旧计时器
            room.current_idiom = word
            room.history.append(word)
            room.round += 1

            # 3. 广播消息
            await self.send_room_message(
                room,
                f"✅ 管理员认可词语 '{word}'，玩家 {target_user_name} 获得 +5 分！\n"
                f"📝 当前词语更新为：{word}\n"
                f"🏆 第{room.round}轮待接：{word}",
            )

            # 4. 重启计时器
            await self.start_round_timer(room)

        except ValueError:
            await self.send_delayed(event, "请输入数字序号，例如 /c p 1")

    # ---------- 机器人主动接龙 ----------

    async def robot_jielong(self, event: AstrMessageEvent, group_id: str):
        room = self.active_games.get(group_id)
        if not room or room.status != "playing":
            await self.send_delayed(event, "当前群没有进行中的游戏")
            return

        curr = room.current_idiom
        last_pinyin = self.get_word_pinyin_flexible(curr, "last")
        if not last_pinyin:
            await self.send_delayed(event, "无法获取当前成语的拼音信息，机器人无法接龙")
            return

        candidates = []
        for word in self.idioms:
            if word in room.history or word == curr:
                continue
            if self.can_chain(curr, word):
                candidates.append(word)

        if not candidates:
            await self.send_delayed(
                event,
                "🤖 机器人找不到可以接龙的成语，游戏可能无法继续。请尝试其他成语或结束游戏。",
            )
            return

        robot_word = random.choice(candidates)
        await self.cancel_timer(room)
        room.current_idiom = robot_word
        room.history.append(robot_word)
        room.round += 1

        await self.send_room_message(
            room,
            f"🤖 机器人接龙成功：{robot_word}\n📝 当前成语：{robot_word}\n🎯 第{room.round}轮待接",
        )
        await self.start_round_timer(room)
        await self.send_delayed(event, f"🤖 机器人已接龙：{robot_word}\n游戏继续！")

    # ---------- 指令处理 ----------
    @filter.command("c")
    async def idiom_main(self, event: AstrMessageEvent):
        # 只支持群聊
        if not event.get_group_id():
            await self.send_delayed(event, "❌ 成语接龙游戏只能在群聊中使用")
            return

        group_id = event.get_group_id()
        message_str = event.message_str.strip()
        args = message_str.split()[1:] if len(message_str.split()) > 1 else []

        if not args:
            help_text = (
                "🐉 成语接龙游戏指令：\n"
                "/c help - 查看游戏规则\n"
                "/c st <四字成语> - 开始游戏（你将成为房主）\n"
                "/c e - 结束游戏（仅房主）\n"
                "/c j - 机器人主动接龙（不增加得分）\n"
                "/c stt <秒数> - 设置全局超时时间（仅房主）\n"
                "/c look - 查看得分排行榜\n"
                "\n💡 游戏中直接发送四字成语即可抢答接龙！"
            )
            await self.send_delayed(event, help_text)
            return

        sub_cmd = args[0].lower()

        if sub_cmd == "help":
            await self.send_delayed(event, self.get_help_text())
        elif sub_cmd == "st":
            await self.start_game(event, group_id, args[1:])
        elif sub_cmd == "e":
            await self.end_game(event, group_id)
        elif sub_cmd == "j":
            await self.robot_jielong(event, group_id)
        elif sub_cmd == "p":
            await self.manage_pending(event, group_id, args[1] if len(args) > 1 else "")
        elif sub_cmd == "stt":
            await self.set_global_timeout(
                event, group_id, args[1] if len(args) > 1 else ""
            )
        elif sub_cmd == "look":
            await self.show_rank(event)
        else:
            await self.send_delayed(
                event, f"❌ 未知指令: {sub_cmd}\n💡 使用 /c 查看帮助"
            )

    async def start_game(self, event: AstrMessageEvent, group_id: str, args: List[str]):
        if group_id in self.active_games:
            await self.send_delayed(
                event, "❌ 当前群已有进行中的游戏，请等待结束后再开始新游戏"
            )
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        if not args:
            await self.send_delayed(event, "请指定开始成语，格式：/c st <四字成语>")
            return

        start_idiom = args[0]
        # 简单验证：四字汉字且在成语库中（可选，也可以接受不在库但给低分，但开始成语建议有效）
        if not self.is_valid_four_chars(start_idiom):
            await self.send_delayed(event, f"❌ 开始成语必须为四字汉字：{start_idiom}")
            return

        # 创建游戏房间
        room = IdiomRoom(group_id, user_id, user_name)
        room.unified_msg_origin = event.unified_msg_origin
        room.current_idiom = start_idiom
        room.history.append(start_idiom)
        room.players[user_id] = Player(user_id, user_name)

        self.active_games[group_id] = room

        await self.send_delayed(
            event,
            f"🐉 成语接龙游戏开始！\n"
            f"📝 开始成语：{start_idiom}\n"
            f"👤 房主：{user_name}\n"
            f"🎯 第1轮待接：{start_idiom}\n"
            f"\n💡 规则：发送四字成语进行抢答接龙！\n"
            f"⚠️ {self.global_timeout_seconds}秒无人接龙则游戏自动结束\n"
            f"🔚 使用 /c e 可结束游戏",
        )
        await self.start_round_timer(room)

    async def end_game(self, event: AstrMessageEvent, group_id: str):
        room = self.active_games.get(group_id)
        if not room:
            await self.send_delayed(event, "当前没有进行中的游戏")
            return

        user_id = event.get_sender_id()
        if not (user_id == room.owner_id or await self.is_admin(user_id)):
            await self.send_delayed(event, "只有房主或管理员可以结束游戏")
            return

        await self.show_final_scores(room)
        self.save_game_history(room)
        await self.end_game_cleanup(room)
        await self.send_delayed(event, "🎮 游戏已结束！使用 /c st 开始新游戏")

    async def set_global_timeout(
        self, event: AstrMessageEvent, group_id: str, timeout_str: str
    ):
        room = self.active_games.get(group_id)
        if not room:
            await self.send_delayed(event, "当前没有进行中的游戏")
            return

        user_id = event.get_sender_id()
        if not (user_id == room.owner_id or await self.is_admin(user_id)):
            await self.send_delayed(event, "只有房主或管理员可以设置超时时间")
            return

        if not timeout_str:
            await self.send_delayed(
                event,
                f"当前全局超时时间：{self.global_timeout_seconds} 秒\n使用 /c stt <秒数> 修改（范围：10-300秒）",
            )
            return

        try:
            new_timeout = int(timeout_str)
            if new_timeout < 10:
                await self.send_delayed(event, "超时时间不能小于10秒")
                return
            if new_timeout > 300:
                await self.send_delayed(event, "超时时间不能大于300秒（5分钟）")
                return

            old_timeout = self.global_timeout_seconds
            self.global_timeout_seconds = new_timeout
            await self.send_room_message(
                room,
                f"⏰ {room.owner_name if user_id == room.owner_id else '管理员'} 将全局超时时间从 {old_timeout} 秒修改为 {new_timeout} 秒",
            )
            await self.send_delayed(
                event, f"✅ 全局超时时间已从 {old_timeout} 秒修改为 {new_timeout} 秒"
            )
        except ValueError:
            await self.send_delayed(event, "请输入有效的秒数，例如：/c stt 60")

    async def show_rank(self, event: AstrMessageEvent):
        if not self.player_scores:
            await self.send_delayed(event, "📊 暂无战绩记录，开始一局游戏吧！")
            return
        sorted_scores = sorted(
            self.player_scores.items(), key=lambda x: x[1]["score"], reverse=True
        )[:10]
        lines = ["🏆 成语接龙排行榜 🏆", ""]
        for i, (uid, data) in enumerate(sorted_scores, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            lines.append(f"{medal} {data['name']}: {data['score']}分")
        await self.send_delayed(event, "\n".join(lines))

    def get_help_text(self) -> str:
        return (
            "🐉 成语接龙游戏规则 🐉\n\n"
            "📋 游戏方式：\n"
            "• 房主使用 /c st <四字成语> 开始游戏\n"
            "• 其他玩家直接发送四字成语进行抢答\n"
            "• 每轮只有第一个接龙成功的玩家得分\n"
            "• 超时后游戏自动结束\n\n"
            "🎯 接龙规则：\n"
            "• 下一个成语的首字拼音（忽略声调）需等于上一个成语的尾字拼音\n"
            "• 相同汉字可直接接龙\n"
            "• 不能重复使用已经出现过的词语\n\n"
            "🏆 得分规则：\n"
            "• 接龙成功且词语在成语库中：+5分\n"
            "• 接龙成功但词语不在成语库中（四字词语）：+2分\n\n"
            "📋 指令列表：\n"
            "/c - 查看本帮助\n"
            "/c st <成语> - 开始游戏\n"
            "/c e - 结束游戏（仅房主）\n"
            "/c j - 机器人主动接龙（不增加得分）\n"
            "/c stt <秒数> - 设置超时时间（仅房主）\n"
            "/c look - 查看得分排行榜\n\n"
            "开始你的成语接龙之旅吧！🚀"
        )

    # ---------- 消息监听 ----------

    @filter.regex(r"^[\u4e00-\u9fff]{4}$")
    async def handle_idiom_input(self, event: AstrMessageEvent):
        if not event.get_group_id():
            return
        group_id = event.get_group_id()
        content = event.message_str.strip()
        await self.process_idiom(event, content, group_id)

    @filter.regex(r".*@.*[\u4e00-\u9fff]{4}.*")
    async def handle_at_idiom_input(self, event: AstrMessageEvent):
        if not event.get_group_id():
            return
        match = re.search(r"[\u4e00-\u9fff]{4}", event.message_str)
        if not match:
            return
        content = match.group(0)
        group_id = event.get_group_id()
        await self.process_idiom(event, content, group_id)

    async def terminate(self):
        for room in self.active_games.values():
            await self.cancel_timer(room)
        if hasattr(self, "cursor"):
            self.cursor.close()
        if hasattr(self, "conn"):
            self.conn.close()
        logger.info("成语接龙插件已卸载")
