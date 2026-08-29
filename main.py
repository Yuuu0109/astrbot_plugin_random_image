import asyncio
import hashlib
import json
import random
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star, StarTools

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# Maximum number of images accepted in a single "添加" message.
MAX_BATCH = 50
# Command words that cannot be used as category names or aliases.
RESERVED_WORDS = {
    "添加",
    "删除",
    "删除分类",
    "别名",
    "列表",
    "每日",
    "取消每日",
    "图库帮助",
}
# Characters unsafe as directory names on common filesystems.
INVALID_CHARS = set('/\\:*?"<>|')
DEFAULT_DAILY_SEND_TIME = "08:00"
DAILY_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class RandomImagePlugin(Star):
    """关键词随机图库(按会话隔离)。

    整条消息精确等于某个分类名或其别名时,随机发送该分类下的一张图片/GIF。
    图库通过中文语句管理:添加(直接带图或引用图片消息)、别名、列表、删除、删除分类。
    每个群聊/私聊拥有独立图库,互不可见。图片按内容 sha256 命名,同分类内自动去重。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.daily_send_time = self._validate_daily_send_time(
            self.config.get("daily_send_time", DEFAULT_DAILY_SEND_TIME),
        )
        self.data_dir = StarTools.get_data_dir()
        self.images_dir = self.data_dir / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.categories_file = self.data_dir / "categories.json"
        self.daily_subscriptions_file = self.data_dir / "daily_subscriptions.json"
        # session key -> {category name -> [aliases]}
        self.categories: dict[str, dict[str, list[str]]] = {}
        # session key -> {exact trigger word -> category name}
        self._trigger_maps: dict[str, dict[str, str]] = {}
        # raw unified_msg_origin -> [category name]
        self.daily_subscriptions: dict[str, list[str]] = {}
        if self.categories_file.exists():
            try:
                self.categories = json.loads(
                    self.categories_file.read_text(encoding="utf-8"),
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"[random_image] failed to load categories.json: {e}")
                self.categories = {}
        if self.daily_subscriptions_file.exists():
            try:
                loaded = json.loads(
                    self.daily_subscriptions_file.read_text(encoding="utf-8"),
                )
                if not isinstance(loaded, dict):
                    raise ValueError("root value must be an object")
                self.daily_subscriptions = {
                    origin: list(dict.fromkeys(categories))
                    for origin, categories in loaded.items()
                    if isinstance(origin, str)
                    and isinstance(categories, list)
                    and all(isinstance(category, str) for category in categories)
                }
            except (json.JSONDecodeError, OSError, ValueError) as e:
                logger.error(
                    f"[random_image] failed to load daily_subscriptions.json: {e}",
                )
                self.daily_subscriptions = {}
        self._rebuild_trigger_maps()
        self._daily_task = asyncio.create_task(
            self._daily_send_loop(),
            name="random_image_daily_sender",
        )

    @staticmethod
    def _validate_daily_send_time(value: object) -> str:
        """Returns a valid HH:MM daily send time, falling back when invalid."""
        candidate = str(value).strip()
        if DAILY_TIME_PATTERN.fullmatch(candidate):
            return candidate
        logger.warning(
            "[random_image] invalid daily_send_time "
            f"{candidate!r}; using {DEFAULT_DAILY_SEND_TIME}",
        )
        return DEFAULT_DAILY_SEND_TIME

    @staticmethod
    def _storage_key_from_origin(origin: str) -> str:
        """Converts a raw unified_msg_origin into the existing storage key."""
        return "".join("_" if c in INVALID_CHARS or c.isspace() else c for c in origin)

    def _session_key(self, event: AstrMessageEvent) -> str:
        """Returns a filesystem-safe storage key for the chat session.

        Args:
            event: The incoming message event.

        Returns:
            A sanitized string derived from event.unified_msg_origin,
            whose raw format is platform_name:message_type:session_id.
        """
        origin = event.unified_msg_origin or "unknown"
        return self._storage_key_from_origin(origin)

    def _rebuild_trigger_maps(self) -> None:
        """Rebuilds the per-session keyword -> category tables from self.categories."""
        self._trigger_maps = {}
        for session, categories in self.categories.items():
            trigger_map = {}
            for category, aliases in categories.items():
                trigger_map.setdefault(category, category)
                for alias in aliases:
                    trigger_map.setdefault(alias, category)
            self._trigger_maps[session] = trigger_map

    def _save_categories(self) -> None:
        """Persists the category mapping to disk and rebuilds the trigger maps."""
        try:
            self.categories_file.write_text(
                json.dumps(self.categories, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"[random_image] failed to save categories.json: {e}")
        self._rebuild_trigger_maps()

    def _save_daily_subscriptions(self) -> None:
        """Persists per-session daily image subscriptions."""
        try:
            self.daily_subscriptions_file.write_text(
                json.dumps(
                    self.daily_subscriptions,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(
                f"[random_image] failed to save daily_subscriptions.json: {e}",
            )

    def _image_files(self, session: str, category: str) -> list[Path]:
        """Returns all supported image files in one session category."""
        cat_dir = self.images_dir / session / category
        if not cat_dir.is_dir():
            return []
        return [
            path
            for path in cat_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTS
        ]

    def _remove_daily_category(self, session: str, category: str) -> bool:
        """Removes a deleted category from every matching raw session origin."""
        changed = False
        for origin, categories in list(self.daily_subscriptions.items()):
            if self._storage_key_from_origin(origin) != session:
                continue
            if category in categories:
                categories.remove(category)
                changed = True
            if not categories:
                self.daily_subscriptions.pop(origin, None)
        return changed

    def _name_error(self, name: str) -> str | None:
        """Validates a category name or alias.

        Args:
            name: The category name or alias to check.

        Returns:
            An error message if the name is invalid, otherwise None.
        """
        if name in RESERVED_WORDS:
            return f"「{name}」是保留指令词"
        if name in {".", ".."} or any(c in INVALID_CHARS for c in name):
            return f"「{name}」含有非法字符"
        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def add_image(self, event: AstrMessageEvent):
        """添加 分类名 [别名...]:添加图片到本会话的分类(消息直接带图,或引用一条含图消息)"""
        parts = event.message_str.split()
        if len(parts) < 2 or parts[0] != "添加":
            return
        session = self._session_key(event)
        category, aliases = parts[1], parts[2:]

        err = self._name_error(category)
        if err:
            yield event.plain_result(f"分类名无效:{err}")
            event.stop_event()
            return

        # Collect images attached to this message plus those inside the
        # quoted (replied-to) message.
        image_segs = [seg for seg in event.get_messages() if isinstance(seg, Image)]
        for seg in event.get_messages():
            if not isinstance(seg, Reply):
                continue
            if seg.chain:
                image_segs += [c for c in seg.chain if isinstance(c, Image)]
            elif seg.id and hasattr(event, "bot"):
                # The adapter failed to expand the quote; fetch it ourselves
                # via the OneBot API (aiocqhttp platforms only).
                try:
                    raw = await event.bot.get_msg(message_id=int(seg.id))
                    for m in raw.get("message", []):
                        if isinstance(m, dict) and m.get("type") == "image":
                            url = (m.get("data") or {}).get("url")
                            if url:
                                image_segs.append(Image.fromURL(url))
                except Exception as e:
                    logger.warning(
                        f"[random_image] failed to fetch quoted message: {e}",
                    )

        if not image_segs:
            yield event.plain_result(
                "没有找到图片。请在发送「添加 分类名」时直接附上图片,"
                "或引用一条包含图片的消息\n示例:添加 a [图片]"
            )
            event.stop_event()
            return

        cat_dir = self.images_dir / session / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        saved = duplicate = failed = 0
        for seg in image_segs[:MAX_BATCH]:
            try:
                src = Path(await seg.convert_to_file_path())
                digest = hashlib.sha256(src.read_bytes()).hexdigest()
                ext = src.suffix.lower()
                if ext not in IMAGE_EXTS:
                    ext = ".jpg"
                dest = cat_dir / f"{digest}{ext}"
                if dest.exists():
                    duplicate += 1
                    continue
                shutil.copy(src, dest)
                saved += 1
            except Exception as e:
                failed += 1
                logger.warning(f"[random_image] failed to save image: {e}")

        # Register the category and every non-conflicting alias in this session.
        trigger_map = self._trigger_maps.setdefault(session, {})
        bound = self.categories.setdefault(session, {}).setdefault(category, [])
        skipped_aliases = []
        for alias in aliases:
            if (
                not self._name_error(alias)
                and alias != category
                and alias not in bound
                and alias not in trigger_map
            ):
                bound.append(alias)
            else:
                skipped_aliases.append(alias)
        self._save_categories()

        reply = f"已向「{category}」添加 {saved} 张图片"
        if duplicate:
            reply += f",跳过重复 {duplicate} 张"
        if failed:
            reply += f",失败 {failed} 张"
        if len(image_segs) > MAX_BATCH:
            reply += f"(单次最多处理 {MAX_BATCH} 张)"
        if aliases:
            ok = [a for a in aliases if a not in skipped_aliases]
            if ok:
                reply += f"\n已绑定别名:{'、'.join(ok)}"
            if skipped_aliases:
                reply += f"\n未生效别名(无效或已被占用):{'、'.join(skipped_aliases)}"
        yield event.plain_result(reply)
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def add_alias(self, event: AstrMessageEvent):
        """别名 分类名 别名...:为本会话的已有分类绑定新的触发词"""
        parts = event.message_str.split()
        if len(parts) < 3 or parts[0] != "别名":
            return
        session = self._session_key(event)
        categories = self.categories.get(session, {})
        category = parts[1]
        if category not in categories:
            yield event.plain_result(f"分类「{category}」不存在")
            event.stop_event()
            return
        trigger_map = self._trigger_maps.setdefault(session, {})
        bound = categories[category]
        added, skipped = [], []
        for alias in parts[2:]:
            if (
                not self._name_error(alias)
                and alias != category
                and alias not in bound
                and alias not in trigger_map
            ):
                bound.append(alias)
                added.append(alias)
            else:
                skipped.append(alias)
        self._save_categories()
        reply = (
            f"已为「{category}」绑定别名:{'、'.join(added)}"
            if added
            else "没有可用的别名"
        )
        if skipped:
            reply += f"\n未生效(无效或已被占用):{'、'.join(skipped)}"
        yield event.plain_result(reply)
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def delete_image(self, event: AstrMessageEvent):
        """删除 分类名 序号:删除本会话该分类下的第 N 张图片(序号见「列表 分类名」)"""
        parts = event.message_str.split()
        if len(parts) != 3 or parts[0] != "删除":
            return
        session = self._session_key(event)
        category = parts[1]
        cat_dir = self.images_dir / session / category
        if category not in self.categories.get(session, {}) or not cat_dir.is_dir():
            yield event.plain_result(f"分类「{category}」不存在")
            event.stop_event()
            return
        files = sorted(
            p
            for p in cat_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        if not files:
            yield event.plain_result(f"分类「{category}」没有图片")
            event.stop_event()
            return
        try:
            index = int(parts[2])
        except ValueError:
            yield event.plain_result("序号需要是数字,发送「列表 分类名」查看序号")
            event.stop_event()
            return
        if not 1 <= index <= len(files):
            yield event.plain_result(f"序号超出范围(1-{len(files)})")
            event.stop_event()
            return
        files[index - 1].unlink()
        yield event.plain_result(f"已删除「{category}」的第 {index} 张图片")
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def delete_category(self, event: AstrMessageEvent):
        """删除分类 分类名:删除本会话的整个分类及其全部图片"""
        parts = event.message_str.split()
        if len(parts) != 2 or parts[0] != "删除分类":
            return
        session = self._session_key(event)
        category = parts[1]
        categories = self.categories.get(session, {})
        if category not in categories:
            yield event.plain_result(f"分类「{category}」不存在")
            event.stop_event()
            return
        del categories[category]
        if not categories:
            # Drop the empty session bucket to keep the store tidy.
            self.categories.pop(session, None)
        self._save_categories()
        if self._remove_daily_category(session, category):
            self._save_daily_subscriptions()
        shutil.rmtree(self.images_dir / session / category, ignore_errors=True)
        yield event.plain_result(f"已删除分类「{category}」及其全部图片")
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def list_library(self, event: AstrMessageEvent):
        """列表:查看本会话的所有分类;列表 分类名:查看该分类的图片序号"""
        parts = event.message_str.split()
        if not parts or parts[0] != "列表" or len(parts) > 2:
            return
        session = self._session_key(event)
        categories = self.categories.get(session, {})
        if len(parts) == 1:
            if not categories:
                yield event.plain_result(
                    "本会话的图库还是空的,发送「添加 分类名 [图片]」创建第一个分类"
                )
                event.stop_event()
                return
            lines = []
            for name, aliases in categories.items():
                cat_dir = self.images_dir / session / name
                count = (
                    sum(
                        1
                        for p in cat_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                    )
                    if cat_dir.is_dir()
                    else 0
                )
                alias_part = f"(别名:{'、'.join(aliases)})" if aliases else ""
                lines.append(f"{name} [{count}张] {alias_part}")
            yield event.plain_result("图库分类:\n" + "\n".join(lines))
            event.stop_event()
            return
        category = parts[1]
        if category not in categories:
            yield event.plain_result(f"分类「{category}」不存在")
            event.stop_event()
            return
        cat_dir = self.images_dir / session / category
        files = (
            sorted(
                p
                for p in cat_dir.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            )
            if cat_dir.is_dir()
            else []
        )
        if not files:
            yield event.plain_result(
                f"分类「{category}」没有图片,发送「添加 {category} [图片]」来添加"
            )
            event.stop_event()
            return
        lines = [
            f"{i}. {p.stem[:8]}.{p.suffix.lstrip('.')}" for i, p in enumerate(files, 1)
        ]
        yield event.plain_result(
            f"分类「{category}」共 {len(files)} 张:\n"
            + "\n".join(lines)
            + f"\n删除某张:删除 {category} 序号"
        )
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def library_help(self, event: AstrMessageEvent):
        """图库帮助:查看关键词随机图库的使用方法"""
        if event.message_str.strip() != "图库帮助":
            return
        yield event.plain_result(
            "📖 关键词随机图库\n\n"
            "· 发送分类名或别名 → 随机发送该分类的一张图片\n"
            "· 添加 分类名 [别名...](附带图片或引用含图消息)→ 添加图片\n"
            "· 别名 分类名 别名... → 绑定新别名\n"
            "· 列表 / 列表 分类名 → 查看图库\n"
            "· 删除 分类名 序号 → 删除单张\n"
            "· 删除分类 分类名 → 删除整个分类\n"
            "· 每日 分类名 → 每天定时发送该分类的一张随机图片\n"
            "· 每日 → 查看本会话已开启的每日分类\n"
            "· 取消每日 分类名 → 停止该分类的每日发送\n\n"
            "图库按会话隔离:每个群聊/私聊拥有独立的分类\n"
            "同一分类内图片按内容自动去重\n"
            f"当前每日发图时间:{self.daily_send_time}"
        )
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def manage_daily_image(self, event: AstrMessageEvent):
        """每日 分类名:开启每日随机图;每日:查看;取消每日 分类名:关闭"""
        parts = event.message_str.split()
        if not parts or parts[0] not in {"每日", "取消每日"}:
            return

        command = parts[0]
        if command == "每日" and len(parts) == 1:
            categories = self.daily_subscriptions.get(
                event.unified_msg_origin or "",
                [],
            )
            if categories:
                yield event.plain_result(
                    f"本会话的每日分类:{'、'.join(categories)}\n"
                    f"全局发送时间:{self.daily_send_time}",
                )
            else:
                yield event.plain_result(
                    "本会话尚未开启每日随机图\n"
                    f"发送「每日 分类名」开启,全局发送时间:{self.daily_send_time}",
                )
            event.stop_event()
            return

        if len(parts) != 2:
            usage = "每日 分类名" if command == "每日" else "取消每日 分类名"
            yield event.plain_result(f"用法:{usage}")
            event.stop_event()
            return

        origin = event.unified_msg_origin
        if not origin:
            yield event.plain_result("无法识别当前会话,请稍后重试")
            event.stop_event()
            return

        session = self._session_key(event)
        requested = parts[1]
        category = self._trigger_maps.get(session, {}).get(requested)
        subscriptions = self.daily_subscriptions.get(origin, [])
        if category is None and command == "取消每日" and requested in subscriptions:
            # Allow a stale subscription to be removed even if its category was
            # manually deleted from the data directory.
            category = requested
        if category is None:
            yield event.plain_result(f"分类或别名「{requested}」不存在")
            event.stop_event()
            return

        if command == "每日":
            if category in subscriptions:
                yield event.plain_result(
                    f"「{category}」已开启每日随机图,发送时间:{self.daily_send_time}",
                )
                event.stop_event()
                return
            subscriptions = self.daily_subscriptions.setdefault(origin, [])
            subscriptions.append(category)
            self._save_daily_subscriptions()
            yield event.plain_result(
                f"已开启「{category}」每日随机图\n"
                f"每天 {self.daily_send_time} 自动发送一张",
            )
            event.stop_event()
            return

        if category not in subscriptions:
            yield event.plain_result(f"「{category}」尚未开启每日随机图")
            event.stop_event()
            return
        subscriptions.remove(category)
        if not subscriptions:
            self.daily_subscriptions.pop(origin, None)
        self._save_daily_subscriptions()
        yield event.plain_result(f"已取消「{category}」每日随机图")
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def send_random_image(self, event: AstrMessageEvent):
        """整条消息等于分类名/别名时,随机发送本会话该分类下的一张图片或 GIF"""
        session = self._session_key(event)
        category = self._trigger_maps.get(session, {}).get(event.message_str.strip())
        if category is None:
            return
        files = self._image_files(session, category)
        if not files:
            yield event.plain_result(
                f"分类「{category}」还没有图片,发送「添加 {category} [图片]」来添加"
            )
            event.stop_event()
            return
        yield event.chain_result([Image.fromFileSystem(str(random.choice(files)))])
        event.stop_event()

    async def _send_daily_images(self) -> None:
        """Sends one random image for every enabled session/category pair."""
        if not self.daily_subscriptions:
            logger.info("[random_image] no daily image subscriptions to send")
            return

        logger.info("[random_image] starting daily image delivery")
        for origin, categories in list(self.daily_subscriptions.items()):
            session = self._storage_key_from_origin(origin)
            for category in list(categories):
                files = self._image_files(session, category)
                if not files:
                    logger.warning(
                        "[random_image] skipped daily image: "
                        f"session={origin}, category={category}, no images found",
                    )
                    continue
                image_path = random.choice(files)
                try:
                    sent = await self.context.send_message(
                        origin,
                        MessageChain().file_image(str(image_path)),
                    )
                    if not sent:
                        logger.warning(
                            "[random_image] daily image platform unavailable: "
                            f"session={origin}, category={category}",
                        )
                except Exception as e:
                    logger.warning(
                        "[random_image] failed to send daily image: "
                        f"session={origin}, category={category}, error={e}",
                    )

    async def _daily_send_loop(self) -> None:
        """Waits until the configured local time and delivers daily images."""
        hour, minute = (int(part) for part in self.daily_send_time.split(":"))
        while True:
            try:
                now = datetime.now()
                target = now.replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0,
                )
                if target <= now:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                logger.info(
                    "[random_image] next daily image delivery: "
                    f"{target.strftime('%Y-%m-%d %H:%M')}",
                )
                await asyncio.sleep(wait_seconds)
                await self._send_daily_images()
            except asyncio.CancelledError:
                logger.info("[random_image] daily image task cancelled")
                raise
            except Exception as e:
                logger.error(f"[random_image] daily image loop failed: {e}")
                await asyncio.sleep(60)

    async def terminate(self):
        """Cancels the daily sender during plugin teardown/reload."""
        if self._daily_task.done():
            return
        self._daily_task.cancel()
        try:
            await self._daily_task
        except asyncio.CancelledError:
            pass
