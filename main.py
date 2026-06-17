import json
import secrets
from pathlib import Path

import astrbot.api.star as star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig

import string

from astrbot.core.message.components import Plain


# ------------------------
# 工具与数据路径
# ------------------------


def load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("❌ 文件不存在！本次创建空 JSON！")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"❌ 文件 {path} 不是有效 JSON: {e}")
        raise ValueError(f"❌ 文件 {path} 不是有效 JSON: {e}") from e
    except OSError as e:
        logger.error(f"❌ 读取文件 {path} 失败: {e}")
        raise RuntimeError(f"❌ 读取文件 {path} 失败: {e}") from e
    except Exception as e:
        logger.error(f"❌ 发生预期外的 JSON 读取错误: {e}！")
        raise RuntimeError(f"❌ 发生预期外的 JSON 读取错误: {e}！")


def save_json(path: Path, data: dict):
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except OSError as e:
        logger.error(f"❌ 写入文件 {path} 失败: {e}")
        raise RuntimeError(f"❌ 写入文件 {path} 失败: {e}") from e
    except TypeError as e:
        logger.error(f"❌ 数据无法序列化为 JSON: {e}")
        raise ValueError(f"❌ 数据无法序列化为 JSON: {e}") from e
    except Exception as e:
        logger.error(f"❌ 发生预期外的 JSON 写入错误: {e}")
        raise RuntimeError(f"❌ 发生预期外的 JSON 写入错误: {e}") from e


def gen_code(n=6):
    # 使用 secrets 模块生成更安全的随机字符串
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))



# ------------------------
# 存储层（无锁简化）
# ------------------------
class MsgForwardStore:
    def __init__(self, pending_file: Path):
        self.pending_file = pending_file
        self._ensure_files()

    def _ensure_files(self):
        if not self.pending_file.exists():
            self.pending_file.write_text("{}", encoding="utf-8")

    # ----- pending -----
    def load_pending(self):
        return load_json(self.pending_file)

    def save_pending(self, data: dict):
        save_json(self.pending_file, data)

    def add_pending(self, code: str, source_umo: str):
        p = self.load_pending()
        p[code] = source_umo
        self.save_pending(p)

    def pop_pending(self, code: str):
        p = self.load_pending()
        if code not in p:
            raise KeyError("绑定码不存在或已使用")
        source_umo = p.pop(code)
        self.save_pending(p)
        return source_umo


# ------------------------
# 插件主体
# ------------------------
class MsgForward(star.Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        self.data_dir = star.StarTools.get_data_dir("msg_forward_cc")
        self.pending_file = self.data_dir / "pending.json"

        self.store = MsgForwardStore(self.pending_file)

    def _format_origin_header(self, event: AstrMessageEvent, umo: str) -> str:
        try:
            _, msg_type, conversation_id = umo.split(":", 2)
        except ValueError:
            msg_type = "Unknown"
            conversation_id = "Unknown"

        source_platform = event.get_platform_name()
        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()

        # 平台友好名称（从配置读取，合并默认值）
        default_map = {
            "aiocqhttp": "QQ",
            "wechatpadpro": "微信",
            "telegram": "Telegram",
            "discord": "Discord",
        }
        platform_map = self.config.get("platform_name_map", {}) or {}
        default_map.update(platform_map)
        source_platform_human = default_map.get(source_platform, source_platform)

        # 消息类型友好名称
        if msg_type == "GroupMessage":
            msg_type_human = "群组"
        elif msg_type == "FriendMessage":
            msg_type_human = "私聊"
        else:
            msg_type_human = "未知类型"

        # 使用配置中的模板
        template = self.config.get("header_template", "").strip()
        if template:
            header = template.format(
                sender_name=sender_name,
                sender_id=sender_id,
                platform=source_platform_human,
                msg_type=msg_type_human,
                conversation_id=conversation_id,
            )
        else:
            header = (
                f"[转发] {sender_name} ({sender_id})\n"
                f"来自 {source_platform_human} 的 {msg_type_human}（ID: {conversation_id}）消息"
            )

        return header

    async def initialize(self):
        logger.info("MsgForward plugin init OK")

    @filter.command_group("mf")
    def mf(self):
        """mf 命令组"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("add")
    async def cmd_add(self, event: AstrMessageEvent):
        """创建一则消息转发绑定的请求"""
        code = gen_code()
        source_umo = str(event.unified_msg_origin)
        self.store.add_pending(code, source_umo)

        yield event.plain_result(
            f"📌 已创建绑定请求\n"
            f"请在目标会话执行：#mf bind {code}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("bind")
    async def cmd_bind(self, event: AstrMessageEvent, code: str):
        """接受一则消息转发绑定的请求"""
        try:
            target_umo = str(event.unified_msg_origin)
            source_umo = self.store.pop_pending(code)
            hide_header = self.config.get("default_hide_header", False)

            rules = list(self.config.get("rules", []))
            rules.append({
                "__template_key": "rule",
                "source_umo": source_umo,
                "target_umo": target_umo,
                "hide_header": hide_header,
            })
            self.config["rules"] = rules
            self.config.save_config()

            idx = len(rules)
            yield event.plain_result(f"✅ 已绑定 #{idx}\n{source_umo} → {target_umo}")
        except Exception as e:
            yield event.plain_result(f"❌ 绑定失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("bindraw")
    async def cmd_bindraw(self, event: AstrMessageEvent, args: str = ""):
        """直接创建转发绑定（格式：#mf bindraw 平台 群号 平台 群号）"""
        PLATFORM_MAP = {
            "qq": "aiocqhttp",
            "wx": "wechatpadpro",
            "tg": "telegram",
            "dc": "discord",
        }

        def build_umo(plat: str, uid: str) -> str:
            plat_lower = plat.lower()
            msg_type = "FriendMessage" if plat_lower.endswith("s") else "GroupMessage"
            plat_key = plat_lower[:-1] if plat_lower.endswith("s") else plat_lower
            platform = PLATFORM_MAP.get(plat_key, plat_key)
            return f"{platform}:{msg_type}:{uid}"

        try:
            parts = (args or "").strip().split()
            if len(parts) != 4:
                yield event.plain_result("❌ 格式错误，用法：#mf bindraw 平台 群号 平台 群号\n例：#mf bindraw qq 654321 wx 123456")
                return
            src_plat, src_id, dst_plat, dst_id = parts
            source_umo = build_umo(src_plat, src_id)
            target_umo = build_umo(dst_plat, dst_id)
            hide_header = self.config.get("default_hide_header", False)

            rules = list(self.config.get("rules", []))
            rules.append({
                "__template_key": "rule",
                "source_umo": source_umo,
                "target_umo": target_umo,
                "hide_header": hide_header,
            })
            self.config["rules"] = rules
            self.config.save_config()

            idx = len(rules)
            yield event.plain_result(f"✅ 已绑定 #{idx}\n{source_umo} → {target_umo}")
        except Exception as e:
            yield event.plain_result(f"❌ 直接绑定失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("del")
    async def cmd_del(self, event: AstrMessageEvent, rid: str):
        """删除一条转发规则（规则编号从 #mf list 查看）"""
        try:
            rules = list(self.config.get("rules", []))
            idx = int(rid) - 1
            if idx < 0 or idx >= len(rules):
                yield event.plain_result(f"❌ 规则 #{rid} 不存在")
                return
            removed = rules.pop(idx)
            self.config["rules"] = rules
            self.config.save_config()
            yield event.plain_result(
                f"🗑️ 已删除规则 #{rid}\n{removed.get('source_umo')} → {removed.get('target_umo')}"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 删除失败: {e}")

    @mf.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出与当前会话相关的所有转发规则"""
        source_umo = str(event.unified_msg_origin)
        rules = self.config.get("rules", [])
        matched = [(idx, r) for idx, r in enumerate(rules, start=1) if r.get("source_umo") == source_umo]
        if not matched:
            yield event.plain_result(f"📭 当前会话 {source_umo} 没有规则")
            return

        lines = [f"📜 当前会话({source_umo}) 的规则："]
        for idx, r in matched:
            hide_status = "🔒" if r.get("hide_header", False) else "🔓"
            lines.append(f"#{idx} {r['source_umo']} → {r['target_umo']} {hide_status}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("hide")
    async def cmd_hide_header(self, event: AstrMessageEvent, rid: str):
        """切换规则的来源信息显示状态（隐藏/显示）"""
        try:
            rules = list(self.config.get("rules", []))
            idx = int(rid) - 1
            if idx < 0 or idx >= len(rules):
                yield event.plain_result(f"❌ 规则 #{rid} 不存在")
                return

            current = rules[idx].get("hide_header", False)
            rules[idx]["hide_header"] = not current
            self.config["rules"] = rules
            self.config.save_config()

            status = "隐藏" if not current else "显示"
            yield event.plain_result(f"✅ 规则 #{rid} 来源信息已{status}")
        except Exception as e:
            yield event.plain_result(f"❌ 操作失败：{e}")

    @mf.command("hidelist")
    async def cmd_header_status(self, event: AstrMessageEvent):
        """列出当前会话规则的来源信息显示状态（允许：显示来源，禁止：隐藏来源）"""
        source_umo = str(event.unified_msg_origin)
        rules = self.config.get("rules", [])
        matched = [(idx, r) for idx, r in enumerate(rules, start=1) if r.get("source_umo") == source_umo]
        if not matched:
            yield event.plain_result("📭 当前会话没有规则")
            return

        allowed = []
        blocked = []

        for idx, r in matched:
            if r.get("hide_header", False):
                blocked.append(f"#{idx} {r['source_umo']} → {r['target_umo']}")
            else:
                allowed.append(f"#{idx} {r['source_umo']} → {r['target_umo']}")

        lines = [f"📋 当前会话({source_umo}) 来源信息状态："]
        if allowed:
            lines.append("\n✅ 允许显示来源：")
            lines.extend(allowed)
        if blocked:
            lines.append("\n🔒 禁止显示来源：")
            lines.extend(blocked)

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mf.command("hidelistall")
    async def cmd_header_status_all(self, event: AstrMessageEvent):
        """查看所有规则的来源信息显示状态（允许：显示来源，禁止：隐藏来源）"""
        rules = self.config.get("rules", [])
        if not rules:
            yield event.plain_result("📭 暂无规则")
            return

        allowed = []
        blocked = []

        for idx, r in enumerate(rules, start=1):
            if r.get("hide_header", False):
                blocked.append(f"#{idx} {r['source_umo']} → {r['target_umo']}")
            else:
                allowed.append(f"#{idx} {r['source_umo']} → {r['target_umo']}")

        lines = ["📋 所有规则来源信息状态："]
        if allowed:
            lines.append("\n✅ 允许显示来源：")
            lines.extend(allowed)
        if blocked:
            lines.append("\n🔒 禁止显示来源：")
            lines.extend(blocked)

        yield event.plain_result("\n".join(lines))

    @mf.command("listall")
    async def cmd_list_all(self, event: AstrMessageEvent):
        """列出所有转发规则"""
        rules = self.config.get("rules", [])
        if not rules:
            yield event.plain_result("📭 暂无规则")
            return

        lines = ["📜 所有转发规则："]
        for idx, r in enumerate(rules, start=1):
            hide_status = "🔒" if r.get("hide_header", False) else "🔓"
            lines.append(
                f"#{idx} {r.get('source_umo', '?')} → {r.get('target_umo', '?')} {hide_status}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def forward_message(self, event: AstrMessageEvent):
        """主转发逻辑"""
        try:
            source_umo = str(event.unified_msg_origin)
            rules = [r for r in self.config.get("rules", []) if r.get("source_umo") == source_umo]
            if not rules:
                return

            message_chain = event.get_messages()

            for rule in rules:
                target = rule.get("target_umo")
                if not target:
                    continue
                try:
                    if rule.get("hide_header", False):
                        new_chain = message_chain
                    else:
                        header = self._format_origin_header(event, source_umo)
                        header += "\n\n\u200b"
                        new_chain = [Plain(text=header)] + message_chain
                    await self.context.send_message(target, event.chain_result(new_chain))
                except ValueError as e:
                    logger.error(f"❌ 不合法的 session 字符串，转发失败: {e}")
                except Exception as e:
                    logger.error(f"❌ 转发失败: {e}")

        except Exception as e:
            logger.error(f"❌ 转发逻辑异常: {e}")

    async def terminate(self):
        logger.info("MsgForward plugin terminated")
