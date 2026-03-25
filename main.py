import socket
import re
import asyncio
from functools import partial
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from ping3 import ping

@register("astrbot_plugin_wol_miko", "Miko", "局域网唤醒工具 V1.0.5", "1.0.5")
class WolPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        if not self.config.get("broadcast"):
            self.config["broadcast"] = "255.255.255.255"
        if not self.config.get("port"):
            self.config["port"] = 9
        if not self.config.get("allowed_users"):
            self.config["allowed_users"] = []
        self._save_config()

    def _save_config(self):
        if hasattr(self.config, 'save_config'):
            self.config.save_config()

    def _is_private_allowed(self, event: AstrMessageEvent) -> bool:
        """检查是否为私聊且用户在白名单中，返回 True 表示允许插件处理"""
        # 判断消息类型：私聊时 group_id 为空字符串
        if event.message_obj.group_id:
            return False  # 群聊不允许
        # 私聊检查白名单
        allowed = self.config.get("allowed_users", [])
        if allowed:
            user_id = event.get_sender_id()
            return user_id in allowed
        # 白名单为空表示不限制
        return True

    async def _ping_device(self, ip: str) -> bool:
        try:
            loop = asyncio.get_running_loop()
            ping_func = partial(ping, ip, timeout=2)
            res = await loop.run_in_executor(None, ping_func)
            return res is not None and res is not False
        except Exception:
            return False

    def _send_magic_packet(self, mac: str) -> bool:
        try:
            clean_mac = re.sub(r'[:\-\.]', '', mac.upper())
            data = bytes.fromhex('FF' * 6) + bytes.fromhex(clean_mac * 16)
            broadcast = self.config.get("broadcast", "255.255.255.255")
            port = int(self.config.get("port", 9))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(data, (broadcast, port))
            logger.info(f"已发送幻包到 MAC: {clean_mac}，广播地址 {broadcast}:{port}")
            return True
        except Exception as e:
            logger.error(f"幻包发送失败: {e}")
            return False

    async def _check_device(self, ip: str, show_details: bool, retries: int = 2) -> str:
        for attempt in range(retries + 1):
            online = await self._ping_device(ip)
            if online:
                if show_details:
                    mac = self.config.get("mac", "")
                    return f"🖥️ 设备状态报告\nIP: {ip}\nMAC: {mac}\n状态: ✅ 在线"
                else:
                    return f"✅ 设备 {ip} 已上线！"
            if attempt < retries:
                await asyncio.sleep(30)
        if show_details:
            mac = self.config.get("mac", "")
            return f"🖥️ 设备状态报告\nIP: {ip}\nMAC: {mac}\n状态: ❌ 离线"
        else:
            return f"❌ 设备 {ip} 仍处于离线状态。"

    @filter.command("绑定")
    async def bind(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event):
            return

        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("用法: /绑定 <MAC> [IP]")
            return

        mac_raw = args[1]
        if not re.match(r'^([0-9A-Fa-f]{2}[:-]?){5}([0-9A-Fa-f]{2})$', mac_raw):
            yield event.plain_result("❌ MAC 地址格式错误！")
            return

        self.config["mac"] = mac_raw
        if len(args) >= 3:
            self.config["ip"] = args[2]
        self._save_config()
        yield event.plain_result(f"✅ 绑定成功！\nMAC: {mac_raw}\nIP: {self.config.get('ip', '未设置')}")

    @filter.command("开机")
    async def wake(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event):
            return

        mac = self.config.get("mac")
        if not mac:
            yield event.plain_result("❌ 请先使用 /绑定 设置设备。")
            return

        ip = self.config.get("ip")
        if not ip:
            yield event.plain_result("❌ 未设置 IP 地址，无法进行状态检测。请先使用 /绑定 设置 IP。")
            return

        if not self._send_magic_packet(mac):
            yield event.plain_result("❌ 唤醒指令发送失败，请检查日志。")
            return

        yield event.plain_result(f"✨ 已发送唤醒包到 {mac}，将在30秒后检查设备状态...")
        result = await self._check_device(ip, show_details=True, retries=2)
        yield event.plain_result(result)

    @filter.command("我的电脑")
    async def status(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event):
            return

        ip = self.config.get("ip")
        if not ip:
            yield event.plain_result("❌ 未设置 IP，无法查询状态。")
            return

        result = await self._check_device(ip, show_details=True, retries=0)
        yield event.plain_result(result)

    @filter.command("局域网唤醒帮助")
    async def help(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event):
            return

        help_text = (
            "📡 局域网唤醒插件帮助\n"
            "——————————————\n\n"
            "🔹 指令说明\n\n"
            "/绑定 <MAC地址> [IP地址]\n"
            "  #绑定需要唤醒的电脑 MAC 和 IP 地址\n"
            "  #IP 用于状态检测\n"
            "  例：\n"
            "/绑定 AA:BB:CC:DD:EE:FF\n"
            "/绑定 AA:BB:CC:DD:EE:FF 192.168.1.100\n\n"
            "/开机\n"
            "  #发送 WOL 唤醒包，并检查电脑在线状态\n\n"
            "/我的电脑\n"
            "  #立即检查电脑在线状态\n"
            "  例：\n"
            "  🖥️ 设备状态报告\n"
            "  IP: 192.168.1.100\n"
            "  MAC: AA:BB:CC:DD:EE:FF\n"
            "  状态: ✅ 在线\n\n"
            "/局域网唤醒帮助\n"
            "  #显示本帮助信息\n\n"
            "🔧 配置项（插件管理界面修改）\n"
            "  mac       # 绑定的 MAC 地址\n"
            "  ip        # 绑定的 IP 地址\n"
            "  broadcast # 广播地址（默认 255.255.255.255）\n"
            "  port      # 端口（默认 9）"
        )
        yield event.plain_result(help_text)