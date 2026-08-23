import hashlib
import json
import random
import shutil
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star, StarTools

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# Maximum number of images accepted in a single "添加" message.
MAX_BATCH = 50
# Command words that cannot be used as category names or aliases.
RESERVED_WORDS = {"添加", "删除", "删除分类", "别名", "列表", "图库帮助"}
# Characters unsafe as directory names on common filesystems.
INVALID_CHARS = set('/\\:*?"<>|')


class RandomImagePlugin(Star):
    """关键词随机图库。

    整条消息精确等于某个分类名或其别名时,随机发送该分类下的一张图片/GIF。
    图库通过中文语句管理:添加(直接带图或引用图片消息)、别名、列表、删除、删除分类。
    图片按内容 sha256 命名,同分类内自动去重。
    """

    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir()
        self.images_dir = self.data_dir / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.categories_file = self.data_dir / "categories.json"
        # category name -> list of aliases
        self.categories: dict[str, list[str]] = {}
        # exact trigger word -> category name
        self._trigger_map: dict[str, str] = {}
        if self.categories_file.exists():
            try:
                self.categories = json.loads(
                    self.categories_file.read_text(encoding="utf-8"),
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"[random_image] failed to load categories.json: {e}")
                self.categories = {}
        self._rebuild_trigger_map()

    def _rebuild_trigger_map(self) -> None:
        """Rebuilds the exact keyword -> category lookup table from self.categories."""
        self._trigger_map = {}
        for category, aliases in self.categories.items():
            self._trigger_map.setdefault(category, category)
            for alias in aliases:
                self._trigger_map.setdefault(alias, category)

    def _save_categories(self) -> None:
        """Persists the category mapping to disk and rebuilds the trigger map."""
        try:
            self.categories_file.write_text(
                json.dumps(self.categories, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"[random_image] failed to save categories.json: {e}")
        self._rebuild_trigger_map()

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
        """添加 分类名 [别名...]:添加图片到分类(消息直接带图,或引用一条含图消息)"""
        parts = event.message_str.split()
        if len(parts) < 2 or parts[0] != "添加":
            return
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

        cat_dir = self.images_dir / category
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

        # Register the category and every non-conflicting alias.
        bound = self.categories.setdefault(category, [])
        skipped_aliases = []
        for alias in aliases:
            if (
                not self._name_error(alias)
                and alias != category
                and alias not in bound
                and alias not in self._trigger_map
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
        """别名 分类名 别名...:为已有分类绑定新的触发词"""
        parts = event.message_str.split()
        if len(parts) < 3 or parts[0] != "别名":
            return
        category = parts[1]
        if category not in self.categories:
            yield event.plain_result(f"分类「{category}」不存在")
            event.stop_event()
            return
        bound = self.categories[category]
        added, skipped = [], []
        for alias in parts[2:]:
            if (
                not self._name_error(alias)
                and alias != category
                and alias not in bound
                and alias not in self._trigger_map
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
        """删除 分类名 序号:删除该分类下的第 N 张图片(序号见「列表 分类名」)"""
        parts = event.message_str.split()
        if len(parts) != 3 or parts[0] != "删除":
            return
        category = parts[1]
        cat_dir = self.images_dir / category
        if category not in self.categories or not cat_dir.is_dir():
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
        """删除分类 分类名:删除整个分类及其全部图片"""
        parts = event.message_str.split()
        if len(parts) != 2 or parts[0] != "删除分类":
            return
        category = parts[1]
        if category not in self.categories:
            yield event.plain_result(f"分类「{category}」不存在")
            event.stop_event()
            return
        del self.categories[category]
        self._save_categories()
        shutil.rmtree(self.images_dir / category, ignore_errors=True)
        yield event.plain_result(f"已删除分类「{category}」及其全部图片")
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def list_library(self, event: AstrMessageEvent):
        """列表:查看所有分类;列表 分类名:查看该分类的图片序号"""
        parts = event.message_str.split()
        if not parts or parts[0] != "列表" or len(parts) > 2:
            return
        if len(parts) == 1:
            if not self.categories:
                yield event.plain_result(
                    "图库还是空的,发送「添加 分类名 [图片]」创建第一个分类"
                )
                event.stop_event()
                return
            lines = []
            for name, aliases in self.categories.items():
                cat_dir = self.images_dir / name
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
        if category not in self.categories:
            yield event.plain_result(f"分类「{category}」不存在")
            event.stop_event()
            return
        cat_dir = self.images_dir / category
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
            "· 删除分类 分类名 → 删除整个分类\n\n"
            "同一分类内图片按内容自动去重"
        )
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def send_random_image(self, event: AstrMessageEvent):
        """整条消息等于分类名/别名时,随机发送该分类下的一张图片或 GIF"""
        category = self._trigger_map.get(event.message_str.strip())
        if category is None:
            return
        cat_dir = self.images_dir / category
        files = (
            [
                p
                for p in cat_dir.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            ]
            if cat_dir.is_dir()
            else []
        )
        if not files:
            yield event.plain_result(
                f"分类「{category}」还没有图片,发送「添加 {category} [图片]」来添加"
            )
            event.stop_event()
            return
        yield event.chain_result([Image.fromFileSystem(str(random.choice(files)))])
        event.stop_event()

    async def terminate(self):
        """Optional plugin teardown hook."""
