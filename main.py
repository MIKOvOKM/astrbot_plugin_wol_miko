import socket
import re
import asyncio
import ipaddress
import json
from functools import partial
from datetime import datetime
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:
    from ping3 import ping
except ImportError:
    raise ImportError("插件依赖项缺失：请执行 pip install ping3 来安装必要依赖。")


@register("astrbot_plugin_wol_miko", "Miko", "局域网唤醒工具 V2.0.0", "2.0.0")
class WolPlugin(Star):
    # 常量定义
    MAX_ADD_PER_CMD = 10
    MAX_NAME_LENGTH = 50
    PING_CONCURRENCY = 5

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        try:
            self.config = config
            self._plugin_name = "astrbot_plugin_wol_miko"

            if "allowed_users" not in self.config:
                self.config["allowed_users"] = []
            if "enable_multi_device" not in self.config:
                self.config["enable_multi_device"] = False
            if "wake_timeout" not in self.config:
                self.config["wake_timeout"] = 90
            if "max_devices" not in self.config:
                self.config["max_devices"] = 20

            self.max_devices = int(self.config.get("max_devices", 20))
            if self.max_devices > 99: self.max_devices = 99
            if self.max_devices < 1: self.max_devices = 1

            self._save_config()

            self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
            self.data_dir.mkdir(parents=True, exist_ok=True)

            self.users_data = self._load_users()
            self.ping_semaphore = asyncio.Semaphore(self.PING_CONCURRENCY)

        except Exception as e:
            logger.error(f"WOL插件初始化异常: {e}")

    def _save_config(self):
        if hasattr(self.config, 'save_config'):
            self.config.save_config()

    def _load_users(self) -> dict:
        file_path = self.data_dir / "users.json"
        if file_path.exists():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载用户数据文件失败: {e}")
        return {}

    def _save_users(self):
        try:
            file_path = self.data_dir / "users.json"
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(self.users_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存用户数据文件失败: {e}")

    def _is_private_allowed(self, event: AstrMessageEvent) -> bool:
        if event.message_obj.group_id:
            return False
        allowed = self.config.get("allowed_users", [])
        if not allowed:
            return False
        return str(event.get_sender_id()) in allowed

    def _get_user_data(self, user_id: str) -> dict:
        if user_id not in self.users_data:
            self.users_data[user_id] = {"devices": [], "one_click_list": []}
            self._save_users()
        return self.users_data[user_id]

    def _update_user_data(self, user_id: str, data: dict):
        self.users_data[user_id] = data
        self._save_users()

    def _validate_mac(self, mac: str) -> bool:
        return re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', mac) is not None

    def _validate_device_ip(self, ip: str) -> bool:
        if ':' in ip: return False
        parts = ip.split('.')
        if len(parts) != 4: return False
        try:
            int_parts = [int(p) for p in parts]
            if not all(0 <= p <= 255 for p in int_parts): return False
            if int_parts[0] == 255 or int_parts[3] == 255:
                return False
            return True
        except ValueError:
            return False

    def _validate_broadcast_ip(self, ip: str) -> bool:
        if ':' in ip: return False
        parts = ip.split('.')
        if len(parts) != 4: return False
        try:
            if not all(0 <= int(p) <= 255 for p in parts): return False
            return True
        except ValueError:
            return False

    def _validate_port(self, port: str) -> bool:
        try:
            p = int(port)
            return 1 <= p <= 65535
        except ValueError:
            return False

    async def _ping_device(self, ip: str) -> bool:
        async with self.ping_semaphore:
            try:
                loop = asyncio.get_running_loop()
                ping_func = partial(ping, ip, timeout=1)
                res = await loop.run_in_executor(None, ping_func)
                return res is not None and res is not False
            except Exception:
                return False

    async def _send_wol(self, mac: str, ip: str = None, port: int = 9, broadcast: str = None) -> bool:
        try:
            clean_mac = re.sub(r'[:\-\.]', '', mac.upper())
            if len(clean_mac) != 12: return False
            mac_bytes = bytes.fromhex(clean_mac)
            magic_packet = b'\xff' * 6 + mac_bytes * 16

            if not port: port = 9
            if not broadcast: broadcast = "255.255.255.255"

            loop = asyncio.get_running_loop()
            tasks = []

            def send_to(target, p):
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        s.sendto(magic_packet, (target, p))
                except:
                    pass

            if ip: tasks.append(loop.run_in_executor(None, send_to, ip, port))
            if ip:
                try:
                    parts = str(ip).split('.')
                    subnet_bcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                    tasks.append(loop.run_in_executor(None, send_to, subnet_bcast, port))
                except:
                    pass

            tasks.append(loop.run_in_executor(None, send_to, broadcast, port))

            if tasks: await asyncio.gather(*tasks)
            return True
        except Exception as e:
            logger.error(f"WOL Error: {e}")
            return False

    @filter.command("唤醒帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        multi_mode = self.config.get("enable_multi_device", False)
        mode = "多设备" if multi_mode else "单设备"

        if multi_mode:
            bind_format = "/绑定 <名称> <MAC> <IP> [端口] [广播]"
            examples = (
                "• 基础绑定:\n  /绑定 台式机 AA:BB:CC:DD:EE:FF 192.168.1.5\n"
                "• 指定参数:\n  /绑定 NAS AA:BB:CC:DD:EE:FF 192.168.1.6 7 192.168.1.255"
            )
        else:
            bind_format = "/绑定 <MAC> <IP> [端口/广播] [广播]"
            examples = (
                "• 基础绑定:\n  /绑定 AA:BB:CC:DD:EE:FF 192.168.1.5\n"
                "• 直接指定广播:\n  /绑定 AA:BB:CC:DD:EE:FF 192.168.1.5 10.0.0.255\n"
                "• 指定端口与广播:\n  /绑定 AA:BB:CC:DD:EE:FF 192.168.1.5 9 10.0.0.255"
            )

        help_text = f"""🌐 局域网唤醒 WOL (仅IPv4)
————————————
当前模式: {mode}

📦 设备管理
{bind_format}
 {examples}

/解绑 [名称]
/设备列表
/查看设备 [名称]

🚀 唤醒操作
/开机 [名称]
/一键唤醒

⚙️ 一键列表
/一键唤醒添加 [名称]
/一键唤醒移除 [名称]
/一键唤醒列表
/一键唤醒清空
————————————
💡 依赖: pip install ping3"""
        yield event.plain_result(help_text)

    @filter.command("绑定")
    async def bind_cmd(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        user_id = str(event.get_sender_id())
        multi_mode = self.config.get("enable_multi_device", False)

        args_raw = event.message_str.strip().split()
        args = []
        if args_raw and args_raw[0] == "绑定":
            args = args_raw[1:]

        if not multi_mode:
            if len(args) < 2 or len(args) > 4:
                yield event.plain_result(
                    "❌ 参数错误\n格式: /绑定 <MAC> <IP> [端口] [广播]\n(注意: 若省略端口直接写广播，系统会自动识别)")
                return
        else:
            if len(args) < 3:
                yield event.plain_result("❌ 参数不足\n格式: /绑定 <名称> <MAC> <IP> [端口] [广播]")
                return

        idx = 0
        name = "默认"

        if multi_mode:
            name = args[idx].strip()
            if not name: yield event.plain_result("❌ 设备名称不能为空"); return
            idx += 1
            if len(name) > self.MAX_NAME_LENGTH:
                yield event.plain_result(f"❌ 名称过长（最大 {self.MAX_NAME_LENGTH} 字符）");
                return

        mac = args[idx].strip()
        if not mac: yield event.plain_result("❌ MAC地址不能为空"); return
        idx += 1

        ip = args[idx].strip()
        if not ip: yield event.plain_result("❌ IP地址不能为空"); return
        idx += 1

        port = 9
        broadcast = None

        if idx < len(args):
            arg3 = args[idx].strip()

            is_port = self._validate_port(arg3)
            is_bcast = self._validate_broadcast_ip(arg3)

            if is_port:
                port = int(arg3)
                idx += 1
                if idx < len(args):
                    arg4 = args[idx].strip()
                    if self._validate_broadcast_ip(arg4):
                        broadcast = arg4
                        idx += 1
                    else:
                        yield event.plain_result(f"❌ 无效的广播地址: {arg4}");
                        return
            elif is_bcast:
                broadcast = arg3
                idx += 1
            else:
                yield event.plain_result(f"❌ 无效参数: {arg3}\n此处应填入 端口(数字) 或 广播地址(IP)");
                return

        if idx < len(args):
            yield event.plain_result("❌ 参数过多，请检查格式");
            return

        if not self._validate_mac(mac):
            yield event.plain_result(f"❌ MAC 格式错误 (示例: AA:BB:CC:DD:EE:FF)");
            return
        if not self._validate_device_ip(ip):
            yield event.plain_result(f"❌ 设备IP格式错误\n注: 仅支持IPv4，且IP不能以 255 开头或结尾");
            return

        user_data = self._get_user_data(user_id)
        devices = user_data.get("devices", [])
        if len(devices) >= self.max_devices:
            yield event.plain_result(f"⚠️ 设备数量已达上限 ({self.max_devices})");
            return
        if any(d['name'] == name for d in devices):
            yield event.plain_result(f"❌ 设备名“{name}”已存在");
            return

        new_device = {"name": name, "mac": mac, "ip": ip, "port": port, "broadcast": broadcast}
        devices.append(new_device)
        user_data["devices"] = devices
        self._update_user_data(user_id, user_data)

        yield event.plain_result(
            f"✅ 绑定成功\n名称: {name}\nIP: {ip}\nMAC: {mac}\n端口: {port}\n广播: {broadcast or '默认'}")

    @filter.command("解绑")
    async def unbind_cmd(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        user_id = str(event.get_sender_id())
        args_raw = event.message_str.strip().split()
        if len(args_raw) < 2:
            devices = self._get_user_data(user_id).get("devices", [])
            if not devices: yield event.plain_result("📭 暂无绑定设备，无需解绑"); return
            # 优化：使用换行符
            names = "\n".join([f"• {d['name']}" for d in devices])
            yield event.plain_result(f"📋 可解绑的设备列表:\n\n{names}\n\n> /解绑 <名称>");
            return

        name = args_raw[1].strip()
        if not name: yield event.plain_result("❌ 名称不能为空"); return

        user_data = self._get_user_data(user_id)
        devices = user_data.get("devices", [])
        one_click_list = user_data.get("one_click_list", [])

        new_devices = [d for d in devices if d['name'] != name]
        if len(new_devices) == len(devices):
            yield event.plain_result(f"❌ 未找到设备: {name}");
            return

        if name in one_click_list: one_click_list.remove(name)
        user_data["devices"] = new_devices
        user_data["one_click_list"] = one_click_list
        self._update_user_data(user_id, user_data)
        yield event.plain_result(f"✅ 已解绑: {name}")

    @filter.command("设备列表")
    async def list_cmd(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        user_id = str(event.get_sender_id())
        devices = self._get_user_data(user_id).get("devices", [])
        if not devices: yield event.plain_result("📭 暂无绑定设备"); return

        msg = "📋 设备列表 (概览)\n————————————"
        for d in devices:
            # 格式：名称 | IP:端口 | MAC
            port_str = f":{d['port']}" if d.get('port') != 9 else ""
            msg += f"\n🖥️ {d['name']}\n  IP: {d['ip']}{port_str}\n  MAC: {d['mac']}"

        msg += f"\n————————————\n💡 输入 /查看设备 <名称> 查看完整配置"
        yield event.plain_result(msg)

    @filter.command("查看设备")
    async def detail_cmd(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        user_id = str(event.get_sender_id())
        args_raw = event.message_str.strip().split()

        # 允许不带参数，如果是单设备模式则默认显示第一个
        target_name = args_raw[1].strip() if len(args_raw) > 1 else ""

        user_data = self._get_user_data(user_id)
        devices = user_data.get("devices", [])

        target_device = None

        if not target_name:
            # 如果没输入名称
            if not devices:
                yield event.plain_result("📭 暂无设备");
                return
            if len(devices) == 1:
                target_device = devices[0]
            else:
                # 优化：使用换行符
                names = "\n".join([f"• {d['name']}" for d in devices])
                yield event.plain_result(f"📋 您绑定了多台设备，请指定名称:\n\n{names}\n\n> /查看设备 <名称>");
                return
        else:
            target_device = next((d for d in devices if d['name'] == target_name), None)
            if not target_device:
                yield event.plain_result(f"❌ 未找到设备: {target_name}");
                return

        # 显示详细信息
        msg = f"🖥️ 设备详情: {target_device['name']}\n"
        msg += "————————————\n"
        msg += f"📡 MAC地址:\n{target_device['mac']}\n"
        msg += f"🌐 设备IP:\n{target_device['ip']}\n"
        msg += f"🚪 端口:\n{target_device.get('port', 9)}\n"
        bcast = target_device.get('broadcast')
        msg += f"📢 广播地址:\n{bcast if bcast else '默认 (255.255.255.255)'}"
        msg += "\n————————————"
        yield event.plain_result(msg)

    @filter.command("开机")
    async def wake_cmd(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        user_id = str(event.get_sender_id())
        args_raw = event.message_str.strip().split()
        args = args_raw[1:] if args_raw and args_raw[0] == "开机" else []
        target_name = args[0].strip() if args else ""

        user_data = self._get_user_data(user_id)
        devices = user_data.get("devices", [])

        if not target_name:
            if not devices:
                yield event.plain_result("📭 暂无设备\n请先使用 /绑定 添加");
                return
            if len(devices) == 1:
                target_device = devices[0]
            else:
                # 优化：使用换行符
                names = "\n".join([f"• {d['name']}" for d in devices])
                yield event.plain_result(f"📋 多台设备，请指定名称:\n\n{names}\n\n> /开机 <名称>");
                return
        else:
            target_device = next((d for d in devices if d['name'] == target_name), None)
            if not target_device:
                yield event.plain_result(f"❌ 未找到设备: {target_name}");
                return

        if not await self._send_wol(target_device['mac'], target_device['ip'], target_device.get('port'),
                                    target_device.get('broadcast')):
            yield event.plain_result("❌ 发送失败，请检查日志");
            return

        yield event.plain_result(f"🚀 正在唤醒 {target_device['name']}...")
        max_time = self.config.get("wake_timeout", 90)
        start = datetime.now()

        while (datetime.now() - start).total_seconds() < max_time:
            if await self._ping_device(target_device['ip']):
                cost = int((datetime.now() - start).total_seconds())
                yield event.plain_result(f"✅ {target_device['name']} 已上线 ({cost}s)");
                return
            await asyncio.sleep(1)
        yield event.plain_result(f"⏰ {target_device['name']} 唤醒超时\n(请检查防火墙或依赖 pip install ping3)")

    @filter.command("一键唤醒")
    async def one_click_wake(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        user_id = str(event.get_sender_id())
        user_data = self._get_user_data(user_id)
        devices_map = {d['name']: d for d in user_data.get("devices", [])}
        one_click_list = user_data.get("one_click_list", [])

        if not one_click_list:
            # 列表为空时的优化交互：显示可用的设备列表
            devices = user_data.get("devices", [])
            msg = "📭 一键唤醒列表为空\n\n"
            if devices:
                msg += "💡 您已绑定以下设备，可添加到列表:\n"
                # 优化：使用换行符
                msg += "\n".join([f"• {d['name']}" for d in devices])
            else:
                msg += "您暂无绑定设备，请先使用 /绑定 添加"

            yield event.plain_result(msg)
            return

        valid_devices = []
        for name in one_click_list:
            if name in devices_map:
                device = devices_map[name]
                await self._send_wol(device['mac'], device['ip'], device.get('port'), device.get('broadcast'))
                valid_devices.append(device)

        if not valid_devices: yield event.plain_result("❌ 列表设备无效"); return
        yield event.plain_result(f"🚀 正在唤醒 {len(valid_devices)} 台设备...")

        results = {d['name']: {'status': 'pending', 'ip': d.get('ip', ''), 'latency': -1} for d in valid_devices}
        start_time = datetime.now()
        max_time = self.config.get("wake_timeout", 90)

        while (datetime.now() - start_time).total_seconds() < max_time:
            tasks, names = [], []
            for name, info in results.items():
                if info['status'] == 'pending' and info['ip']:
                    tasks.append(self._ping_device(info['ip']))
                    names.append(name)
            if not tasks or all(r['status'] == 'success' or not r['ip'] for r in results.values()):
                break
            ping_res = await asyncio.gather(*tasks, return_exceptions=True)
            for idx, ok in enumerate(ping_res):
                if ok is True:
                    n = names[idx]
                    results[n]['status'] = 'success'
                    results[n]['latency'] = round((datetime.now() - start_time).total_seconds(), 1)
            await asyncio.sleep(1)

        success = [n for n, i in results.items() if i['status'] == 'success']
        fail = [n for n, i in results.items() if i['status'] == 'pending']
        cost = round((datetime.now() - start_time).total_seconds(), 1)

        msg = f"🚀 唤醒报告 (耗时 {cost}s)\n————————————"
        if success: msg += f"\n✅ 成功 ({len(success)}):\n" + "\n".join(
            [f"{n} ({results[n]['latency']}s)" for n in success])
        if fail: msg += f"\n❌ 失败 ({len(fail)}):\n" + "\n".join(fail)
        yield event.plain_result(msg)

    @filter.command("一键唤醒添加")
    async def one_click_add(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        user_id = str(event.get_sender_id())
        args_raw = event.message_str.strip().split()
        args = args_raw[1:] if args_raw and args_raw[0] == "一键唤醒添加" else []

        if not args:
            devices = self._get_user_data(user_id).get("devices", [])
            if not devices:
                yield event.plain_result("📭 暂无设备\n请先使用 /绑定 添加")
                return
            # 优化：使用换行符
            names = "\n".join([f"• {d['name']}" for d in devices])
            yield event.plain_result(f"📋 可用设备列表:\n\n{names}\n\n> /一键唤醒添加 <名称>")
            return

        if len(args) > self.MAX_ADD_PER_CMD:
            yield event.plain_result(f"⚠️ 单次最多添加 {self.MAX_ADD_PER_CMD} 个设备");
            return

        user_data = self._get_user_data(user_id)
        devices = {d['name'] for d in user_data.get("devices", [])}
        ocl = user_data.get("one_click_list", [])

        if len(ocl) >= self.max_devices:
            yield event.plain_result(f"⚠️ 列表已满 (上限 {self.max_devices} 个)");
            return

        success, dup, notfound, invalid = [], [], [], []
        for name in args:
            name = name.strip()
            if not name: continue
            if len(name) > self.MAX_NAME_LENGTH:
                invalid.append(f"{name[:20]}...")
                continue
            if name not in devices:
                notfound.append(name)
            elif name in ocl:
                dup.append(name)
            elif len(ocl) >= self.max_devices:
                break
            else:
                ocl.append(name)
                success.append(name)

        if success:
            user_data["one_click_list"] = ocl
            self._update_user_data(user_id, user_data)

        res = []
        if success: res.append(f"✅ {', '.join(success)}")
        if dup: res.append(f"⚠️ 重复: {', '.join(dup)}")
        if notfound: res.append(f"❌ 无效: {', '.join(notfound)}")
        if invalid: res.append(f"❌ 过长: {', '.join(invalid)}")
        yield event.plain_result("\n".join(res))

    @filter.command("一键唤醒移除")
    async def one_click_remove(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        user_id = str(event.get_sender_id())
        args_raw = event.message_str.strip().split()
        args = args_raw[1:] if args_raw and args_raw[0] == "一键唤醒移除" else []
        if not args:
            ocl = self._get_user_data(user_id).get("one_click_list", [])
            if not ocl:
                yield event.plain_result("📭 一键唤醒列表为空");
                return
            # 优化：使用换行符
            names = "\n".join([f"• {name}" for name in ocl])
            yield event.plain_result(f"📋 当前一键唤醒列表:\n\n{names}\n\n> /一键唤醒移除 <名称>");
            return

        user_data = self._get_user_data(user_id)
        ocl = user_data.get("one_click_list", [])
        removed = [name for name in args if name.strip() in ocl]
        for name in removed: ocl.remove(name)
        user_data["one_click_list"] = ocl
        self._update_user_data(user_id, user_data)
        if removed:
            yield event.plain_result(f"✅ 已移除: {', '.join(removed)}")
        else:
            yield event.plain_result("⚠️ 未找到匹配设备")

    @filter.command("一键唤醒列表")
    async def one_click_list(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        ocl = self._get_user_data(str(event.get_sender_id())).get("one_click_list", [])
        if not ocl: yield event.plain_result("📭 列表为空"); return
        # 优化：使用换行符
        yield event.plain_result("🚀 一键唤醒列表\n" + "\n".join(ocl))

    @filter.command("一键唤醒清空")
    async def one_click_clear(self, event: AstrMessageEvent):
        if not self._is_private_allowed(event): return
        uid = str(event.get_sender_id())
        self._get_user_data(uid)["one_click_list"] = []
        self._update_user_data(uid, self._get_user_data(uid))
        yield event.plain_result("🗑️ 列表已清空")
