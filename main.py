import socket
import re
import asyncio
import ipaddress
from functools import partial

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from ping3 import ping


@register("astrbot_plugin_wol_miko", "Miko", "局域网唤醒工具 V1.0.0", "1.0.0")
class WolPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        try:
            self.config = config
            if not self.config.get("broadcast"):
                self.config["broadcast"] = "255.255.255.255"
            if not self.config.get("port"):
                self.config["port"] = 9
            if not self.config.get("allowed_users"):
                self.config["allowed_users"] = []
        except Exception as e:
            logger.error(f"WOL插件初始化异常(可能导致指令无效): {e}")

    def _save_config(self):
        if hasattr(self.config, 'save_config'):
            self.config.save_config()

    def _is_private_allowed(self, event: AstrMessageEvent) -> bool:
        """检查是否为私聊且用户在白名单中。"""
        if event.message_obj.group_id:
            return False
        allowed = self.config.get("allowed_users", [])
        if not allowed:
            logger.warning("WOL插件白名单为空，已拒绝来自 %s 的操作。请配置 allowed_users 项。", event.get_sender_id())
            return False
        user_id = event.get_sender_id()
        return user_id in allowed

    async def _ping_device(self, ip: str) -> bool:
        """异步ping设备"""
        try:
            loop = asyncio.get_running_loop()
            ping_func = partial(ping, ip, timeout=2)
            res = await loop.run_in_executor(None, ping_func)
            return res is not None and res is not False
        except Exception as e:
            logger.error(f"Ping 测试异常: {e} (IP: {ip})")
            return False

    async def _send_magic_packet(self, mac: str) -> bool:
        """异步发送幻包：单播与广播双发"""
        try:
            clean_mac = re.sub(r'[:\-\.]', '', mac.upper())
            data = bytes.fromhex('FF' * 6) + bytes.fromhex(clean_mac * 16)
            port = int(self.config.get("port", 9))
            loop = asyncio.get_running_loop()
            device_ip = self.config.get("ip")
            broadcast = self.config.get("broadcast", "255.255.255.255")

            tasks = []
            if device_ip:
                def _unicast():
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.sendto(data, (device_ip, port))

                tasks.append(loop.run_in_executor(None, _unicast))

            def _broadcast():
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.sendto(data, (broadcast, port))

            tasks.append(loop.run_in_executor(None, _broadcast))

            await asyncio.gather(*tasks)
            logger.info(f"✅ 已发送幻包到 MAC: {clean_mac} (目标IP: {device_ip or '未设置'}, 广播: {broadcast}:{port})")
            return True
        except Exception as e:
            logger.error(f"❌ 幻包发送失败: {e}")
            return False

    async def _check_device(self, ip: str, mac: str) -> str:
        """纯检测一次状态并返回格式化字符串"""
        online = await self._ping_device(ip)
        if online:
            return f"🖥️ 设备状态报告\nIP: {ip}\nMAC: {mac}\n状态: ✅ 在线"
        return f"🖥️ 设备状态报告\nIP: {ip}\nMAC: {mac}\n状态: ❌ 离线"

    @filter.command("绑定")
    async def bind(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event):
            yield event.plain_result("❌ 无权限：该插件仅限白名单用户在私聊中使用。")
            return

        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("用法: /绑定 <MAC> [IP]")
            return

        mac_raw = args[1]
        clean_check = re.sub(r'[:\-\.]', '', mac_raw.upper())
        if not re.match(r'^[0-9A-F]{12}$', clean_check):
            yield event.plain_result("❌ MAC 地址格式错误！应为12位十六进制，如 AA:BB:CC:DD:EE:FF")
            return

        ip = None
        if len(args) >= 3:
            ip_raw = args[2]
            try:
                ipaddress.ip_address(ip_raw)
                ip = ip_raw
            except ValueError:
                yield event.plain_result(f"❌ IP 地址格式错误：{ip_raw}")
                return

        self.config["mac"] = mac_raw
        if ip is not None:
            self.config["ip"] = ip
        self._save_config()

        yield event.plain_result(f"✅ 绑定成功！\nMAC: {mac_raw}\nIP: {self.config.get('ip', '未设置')}")

    @filter.command("开机")
    async def wake(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event):
            yield event.plain_result("❌ 无权限：该插件仅限白名单用户在私聊中使用。")
            return

        mac = self.config.get("mac")
        if not mac:
            yield event.plain_result("❌ 请先使用 /绑定 设置设备。")
            return

        if not await self._send_magic_packet(mac):
            yield event.plain_result("❌ 唤醒指令发送失败，请检查日志。")
            return

        ip = self.config.get("ip")
        if ip:
            yield event.plain_result(f"✨ 已发送唤醒包到 {mac}，系统正在启动中，开始检测状态...")

            max_retries = 2  # 总共尝试 3 次 (0s, 30s, 60s)
            for attempt in range(max_retries + 1):
                online = await self._ping_device(ip)
                if online:
                    yield event.plain_result(f"🖥️ 设备状态报告\nIP: {ip}\nMAC: {mac}\n状态: ✅ 在线")
                    return

                # 如果没成功，且还有下一次机会，就发进度提示
                if attempt < max_retries:
                    wait_sec = 30
                    remaining_attempts = max_retries - attempt
                    yield event.plain_result(f"⏳ 等待 {wait_sec} 秒后重试... (剩余检测次数: {remaining_attempts})")
                    await asyncio.sleep(wait_sec)

            # 循环结束还没上线
            yield event.plain_result(
                f"🖥️ 设备状态报告\nIP: {ip}\nMAC: {mac}\n状态: ❌ 离线 (可能开机较慢，请稍后使用 /我的电脑 手动查看)")
        else:
            yield event.plain_result(f"✨ 已发送唤醒包到 {mac}。\n⚠️ 未绑定 IP 地址，无法自动检查状态，请自行确认。")

    @filter.command("我的电脑")
    async def status(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event):
            yield event.plain_result("❌ 无权限：该插件仅限白名单用户在私聊中使用。")
            return

        ip = self.config.get("ip")
        if not ip:
            yield event.plain_result("❌ 未设置 IP，无法查询状态。")
            return

        mac = self.config.get("mac", "")
        result = await self._check_device(ip, mac)
        yield event.plain_result(result)

    @filter.command("局域网唤醒帮助")
    async def help(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event):
            yield event.plain_result("❌ 无权限：该插件仅限白名单用户在私聊中使用。")
            return

        help_text = (
            "📡 局域网唤醒插件帮助\n"
            "——————————————\n\n"
            "🔹 指令说明\n\n"
            "/绑定 <MAC地址> [IP地址]\n"
            " #绑定需要唤醒的电脑 MAC 和 IP 地址\n"
            " #IP 用于状态检测\n"
            " 例：\n"
            "/绑定 AA:BB:CC:DD:EE:FF\n"
            "/绑定 AA:BB:CC:DD:EE:FF 192.168.1.100\n\n"
            "/开机\n"
            " #发送 WOL 唤醒包，自动等待并最多检测3次状态(适配慢速启动)\n\n"
            "/我的电脑\n"
            " #立即检查电脑在线状态\n\n"
            "/局域网唤醒帮助\n"
            " #显示本帮助信息\n\n"
            "🔧 配置项（插件管理界面修改）\n"
            " mac # 绑定的 MAC 地址\n"
            " ip # 绑定的 IP 地址\n"
            " broadcast # 广播地址（默认 255.255.255.255）\n"
            " port # 端口（默认 9）\n\n"
            "⚠️ 注意：请务必在插件配置中设置 allowed_users 白名单（用户ID列表），否则无法使用任何命令。"
        )
        yield event.plain_result(help_text)
