import json
import os
import secrets
from pathlib import Path

import astrbot.api.star as star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig

import string

from astrbot.core.message.components import BaseMessageComponent, Plain


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


def format_origin_header(event: AstrMessageEvent, umo: str):
    try:
        _, msg_type, conversation_id = umo.split(":", 2)
    except ValueError:
        msg_type = "Unknown"
        conversation_id = "Unknown"

    source_platform = event.get_platform_name()
    sender_name = event.get_sender_name()
    sender_id = event.get_sender_id()

    # 平台友好名称
    source_platform_map = {
        "aiocqhttp": "QQ",
        "wechatpadpro": "微信",
        "telegram": "Telegram",
        "discord": "Discord",
    }
    source_platform_human = source_platform_map.get(source_platform, source_platform)

    # 消息类型友好名称
    if msg_type == "GroupMessage":
        msg_type_human = f"群组（ID: {conversation_id}）消息"
    elif msg_type == "FriendMessage":
        msg_type_human = f"私聊（对方 ID: {conversation_id}）消息"
    else:
        msg_type_human = f"未知类型（ID: {conversation_id}）消息"

    return (
        f"[转发] {sender_name} ({sender_id})\n"
        f"来自 {source_platform_human} 的 {msg_type_human}"
    )


# ------------------------
# 存储层（无锁简化）
# ------------------------
class MsgTransferStore:
    def __init__(self, rule_file: Path, pending_file: Path):
        self.rule_file = rule_file
        self.pending_file = pending_file
        self._ensure_files()

    def _ensure_files(self):
        if not self.rule_file.exists():
            self.rule_file.write_text("{}", encoding="utf-8")
        if not self.pending_file.exists():
            self.pending_file.write_text("{}", encoding="utf-8")

    # ----- rules -----
    def load_rules(self):
        return load_json(self.rule_file)

    def save_rules(self, data: dict):
        save_json(self.rule_file, data)

    def add_rule(self, source_umo: str, target_umo: str, hide_header: bool = False) -> str:
        data = self.load_rules()

        # 查重
        for rid, rule in data.items():
            if rule["source_umo"] == source_umo and rule["target_umo"] == target_umo:
                raise ValueError(f"规则已存在 #{rid}")

        new_id = str(max(map(int, data.keys()), default=0) + 1)
        data[new_id] = {
            "source_umo": source_umo,
            "target_umo": target_umo,
            "hide_header": hide_header
        }
        self.save_rules(data)
        return new_id

    def delete_rule(self, rid: str):
        data = self.load_rules()
        if rid not in data:
            raise KeyError("规则不存在")
        data.pop(rid)
        self.save_rules(data)

    def set_hide_header(self, rid: str, hide: bool):
        data = self.load_rules()
        if rid not in data:
            raise KeyError("规则不存在")
        data[rid]["hide_header"] = hide
        self.save_rules(data)

    def list_rules(self, source_umo):
        data = self.load_rules()
        return {rid: r for rid, r in data.items() if r["source_umo"] == source_umo}

    def list_all_rules(self):
        return self.load_rules()

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
class MsgTransfer(star.Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 使用 AstrBot 提供的标准方法获取项目持久化数据存储目录
        self.data_dir = star.StarTools.get_data_dir("msg_transfer")
        self.rule_file = self.data_dir / "rules.json"
        self.pending_file = self.data_dir / "pending.json"

        self.store = MsgTransferStore(self.rule_file, self.pending_file)

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
        logger.info("MsgTransfer plugin init OK")

    @filter.command_group("mt")
    def mt(self):
        """mt 命令组"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mt.command("add")
    async def cmd_add(self, event: AstrMessageEvent):
        """创建一则消息转发绑定的请求"""
        code = gen_code()
        source_umo = str(event.unified_msg_origin)
        self.store.add_pending(code, source_umo)

        yield event.plain_result(
            f"📌 已创建绑定请求\n"
            f"请在目标会话执行：#mt bind {code}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mt.command("bind")
    async def cmd_bind(self, event: AstrMessageEvent, code: str):
        """接受一则消息转发绑定的请求"""
        try:
            target_umo = str(event.unified_msg_origin)
            source_umo = self.store.pop_pending(code)
            hide_header = self.config.get("default_hide_header", False)
            rid = self.store.add_rule(source_umo, target_umo, hide_header)
            yield event.plain_result(f"✅ 已绑定 #{rid}\n{source_umo} → {target_umo}")
        except Exception as e:
            yield event.plain_result(f"❌ 绑定失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mt.command("del")
    async def cmd_del(self, event: AstrMessageEvent, rid: str):
        """删除一条转发规则"""
        try:
            self.store.delete_rule(rid)
            yield event.plain_result(f"🗑️ 已删除规则 #{rid}")
        except Exception as e:
            yield event.plain_result(f"❌ 删除失败: {e}")

    @mt.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出与当前会话相关的所有转发规则"""
        source_umo = str(event.unified_msg_origin)
        rules = self.store.list_rules(source_umo)
        if not rules:
            yield event.plain_result("📭 当前会话没有规则")
            return

        lines = [f"📜 当前会话({source_umo}) 的规则："]
        for rid, r in rules.items():
            hide_status = "🔒" if r.get("hide_header", False) else "🔓"
            lines.append(f"#{rid} {r['source_umo']} → {r['target_umo']} {hide_status}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mt.command("hide")
    async def cmd_hide_header(self, event: AstrMessageEvent, rid: str):
        """切换规则的来源信息显示状态（隐藏/显示）"""
        try:
            data = self.store.load_rules()
            if rid not in data:
                yield event.plain_result(f"❌ 规则 #{rid} 不存在")
                return

            current = data.get(rid, {}).get("hide_header", False)
            self.store.set_hide_header(rid, not current)
            status = "隐藏" if not current else "显示"
            yield event.plain_result(f"✅ 规则 #{rid} 来源信息已{status}")
        except Exception as e:
            yield event.plain_result(f"❌ 操作失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mt.command("header")
    async def cmd_header_status(self, event: AstrMessageEvent):
        """查看所有规则的来源信息显示状态（允许：显示来源，禁止：隐藏来源）"""
        all_rules = self.store.list_all_rules()
        if not all_rules:
            yield event.plain_result("📭 暂无规则")
            return

        allowed = []
        blocked = []

        for rid, r in all_rules.items():
            if r.get("hide_header", False):
                blocked.append(f"#{rid} {r['source_umo']} → {r['target_umo']}")
            else:
                allowed.append(f"#{rid} {r['source_umo']} → {r['target_umo']}")

        lines = ["📋 规则来源信息状态列表："]
        if allowed:
            lines.append("\n✅ 允许显示来源：")
            lines.extend(allowed)
        if blocked:
            lines.append("\n🔒 禁止显示来源：")
            lines.extend(blocked)

        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def forward_message(self, event: AstrMessageEvent):
        """主转发逻辑"""
        try:
            source_umo = str(event.unified_msg_origin)
            rules = self.store.list_rules(source_umo)
            if not rules:
                return

            message_chain = event.get_messages()

            for rid, rule in rules.items():
                target = rule["target_umo"]
                try:
                    if rule.get("hide_header", False):
                        new_chain = message_chain
                    else:
                        header = self._format_origin_header(event, source_umo)
                        header += "\n\n\u200b"
                        new_chain = list[BaseMessageComponent]([Plain(text=header)]) + message_chain
                    await self.context.send_message(target, event.chain_result(new_chain))
                except ValueError as e:
                    logger.error(f"❌ 不合法的 session 字符串，转发失败 #{rid}: {e}")
                except Exception as e:
                    logger.error(f"❌ 转发失败 #{rid}: {e}")

        except Exception as e:
            logger.error(f"❌ 转发逻辑异常: {e}")

    async def terminate(self):
        logger.info("MsgTransfer plugin terminated")
