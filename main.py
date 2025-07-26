# main.py
import asyncio
import asyncssh
import json
import os
import tempfile
import subprocess   # 用于 ping
import time
from datetime import datetime, timedelta
from typing import Optional
from astrbot.api.event import filter, EventMessageType, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register(
    "server_monitor",
    "Kimi",
    "远程服务器监控与报警插件（增强版）",
    "1.0.2",
    "https://github.com/yourusername/astrbot_plugin_server_monitor"
)
class ServerMonitorPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.last_success: Optional[datetime] = None
        self.connected = False
        self._reconnect_lock = asyncio.Lock()
        self._current_config_digest = self._digest_config()

        asyncio.create_task(self.start_monitoring())

    # ---------- 工具 ----------
    def _digest_config(self) -> str:
        """返回配置摘要，用于检测变动"""
        return json.dumps(
            [self.config.get(k) for k in
             ("server_ip", "ssh_port", "ssh_username", "ssh_password", "ssh_key_path")],
            sort_keys=True
        )

    def _save_config(self):
        cfg_path = os.path.join("data", "config", "server_monitor_config.json")
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    async def send_message(self, target: str, message: str):
        await self.context.send_message(target, [message])

    # ---------- 即时配置变动触发 ----------
    async def _on_config_changed(self, event: AstrMessageEvent | None = None):
        """配置变动后立刻尝试连接"""
        async with self._reconnect_lock:
            ip = self.config.get("server_ip")
            if not ip:
                return
            # 1. ping 测试
            ping_ok, rtt = await self._ping(ip)
            if event:
                await event.send(event.plain_result(
                    f"🛜 ping {ip}: " + (f"{rtt:.1f} ms" if ping_ok else "超时/丢包")
                ))
            if not ping_ok:
                if event:
                    await event.send(event.plain_result("❌ 网络不可达，终止 SSH 连接尝试"))
                return

            # 2. 带策略的 SSH 连接
            ok, msg = await self._connect_with_retry()
            if event:
                await event.send(event.plain_result(msg))

    # ---------- ping ----------
    async def _ping(self, host: str) -> tuple[bool, float]:
        """返回 (是否可达, RTT ms)"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "3", "-W", "1", host,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            lines = stdout.decode().splitlines()
            for line in lines:
                if "avg" in line or "average" in line:
                    # Linux: rtt min/avg/max/mdev = 12.3/15.6/18.9/2.4 ms
                    try:
                        rtt = float(line.split("/")[4])
                        return True, rtt
                    except Exception:
                        pass
            return False, 0.0
        except Exception:
            return False, 0.0

    # ---------- 连接策略 ----------
    async def _connect_with_retry(self) -> tuple[bool, str]:
        """最多 4 次重连，2 分钟总时长，单次 1 min 超时"""
        attempts = 0
        start = datetime.now()
        while attempts < 4 and (datetime.now() - start) < timedelta(seconds=120):
            try:
                async with asyncssh.connect(
                    self.config["server_ip"],
                    port=int(self.config.get("ssh_port", 22)),
                    username=self.config["ssh_username"],
                    password=self.config.get("ssh_password") or None,
                    client_keys=[self.config.get("ssh_key_path")] if self.config.get("ssh_key_path") else None,
                    known_hosts=None,
                    connect_timeout=60
                ):
                    self.connected = True
                    self.last_success = datetime.now()
                    return True, "✅ SSH 连接成功"
            except Exception as e:
                attempts += 1
                logger.warning(f"SSH 连接第 {attempts}/4 次失败: {e}")
                if attempts < 4:
                    await asyncio.sleep(15)   # 间隔 15s
        self.connected = False
        return False, "❌ 连续 4 次连接失败，请检查配置"

    # ---------- 定时任务 ----------
    async def start_monitoring(self):
        await self.schedule_daily_report()
        await self.schedule_health_check()

    async def schedule_daily_report(self):
        while True:
            now = datetime.now()
            try:
                target_time = datetime.strptime(self.config["report_time"], "%H:%M").time()
            except ValueError:
                logger.error("report_time 格式错误")
                await asyncio.sleep(3600)
                continue

            if now.time() >= target_time:
                await asyncio.sleep(86400 - (now.hour * 3600 + now.minute * 60 + now.second))
            else:
                wait_seconds = (target_time.hour * 3600 + target_time.minute * 60) - \
                               (now.hour * 3600 + now.minute * 60 + now.second)
                await asyncio.sleep(wait_seconds)

            await self._push_status(self.config["report_target"])

    async def schedule_health_check(self):
        while True:
            await asyncio.sleep(300)
            status = await self.get_server_status()
            if status:
                self.connected = True
                self.last_success = datetime.now()
            else:
                if self.connected and self.last_success and \
                   (datetime.now() - self.last_success).total_seconds() > 1200:
                    await self.send_message(self.config["alert_target"], "🚨 服务器断联超过20分钟！")
                    self.connected = False

    # ---------- 统一状态推送 ----------
    async def _push_status(self, target: str):
        status = await self.get_server_status()
        if status:
            await self.send_message(target, f"📊 服务器状态报告：\n{status}")
        else:
            await self.send_message(target, "❌ 无法连接服务器")

    # ---------- 指令组 /server ----------
    @filter.command_group("server")
    def server_group(self):
        """服务器管理指令组"""
        pass

    @server_group.command("status")
    async def server_status(self, event: AstrMessageEvent):
        """查看服务器实时连接状态"""
        status = await self.get_server_status()
        if status:
            yield event.plain_result(f"✅ 服务器连接正常：\n{status}")
        else:
            yield event.plain_result("❌ 服务器连接失败")

    @server_group.command("push")
    async def server_push(self, event: AstrMessageEvent):
        """手动触发一次状态推送"""
        await self._push_status(event.unified_msg_origin)

    # ---------- 动态配置 /server set ----------
    @server_group.command("set")
    async def server_set(self, event: AstrMessageEvent, key: str, value: str):
        """/server set <key> <value>  动态修改配置"""
        key = key.lower()
        valid_keys = {
            "ip": "server_ip",
            "port": "ssh_port",
            "user": "ssh_username",
            "pwd": "ssh_password",
            "key": "ssh_key_path",
            "report_time": "report_time",
            "report_target": "report_target",
            "alert_target": "alert_target"
        }
        if key not in valid_keys:
            yield event.plain_result("❌ 未知键值")
            return

        real_key = valid_keys[key]

        # 类型转换
        if real_key == "ssh_port":
            try:
                value = int(value)
            except ValueError:
                yield event.plain_result("❌ 端口必须是整数")
                return

        # 私钥文本 → 临时文件
        if real_key == "ssh_key_path":
            fd, temp_path = tempfile.mkstemp(prefix="srv_key_", suffix=".pem", text=True)
            try:
                os.write(fd, value.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(temp_path, 0o600)
            value = temp_path
            old_path = self.config.get("ssh_key_path", "")
            if old_path.startswith(tempfile.gettempdir()):
                try:
                    os.remove(old_path)
                except Exception:
                    pass

        # 更新配置
        old_digest = self._current_config_digest
        self.config[real_key] = value
        self._save_config()
        yield event.plain_result(f"✅ 已更新 {real_key}")

        # 配置变动后触发重连
        new_digest = self._digest_config()
        if new_digest != old_digest:
            asyncio.create_task(self._on_config_changed(event))

    @server_group.command("show")
    async def server_show(self, event: AstrMessageEvent):
        """查看当前配置"""
        lines = ["📄 当前配置："]
        for k in ("server_ip", "ssh_port", "ssh_username", "ssh_password",
                  "ssh_key_path", "report_time", "report_target", "alert_target"):
            v = self.config.get(k, "未设置")
            if k == "ssh_password" and v:
                v = "***"
            lines.append(f"{k}: {v}")
        yield event.plain_result("\n".join(lines))
