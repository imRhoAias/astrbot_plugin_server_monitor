# main.py
import asyncio
import asyncssh
import json
import os
import tempfile
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register(
    "server_monitor",
    "imRhoAias",
    "远程服务器监控与报警插件",
    "1.0.0",
    "https://github.com/imRhoAias/astrbot_plugin_server_monitor"
)
class ServerMonitorPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.last_success = None
        self.connected = False
        asyncio.create_task(self.start_monitoring())

    # ---------------- 启动定时任务 ----------------
    async def start_monitoring(self):
        await self.schedule_daily_report()
        await self.schedule_health_check()

    # ---------------- SSH 状态获取 ----------------
    async def get_server_status(self) -> str | None:
        try:
            conn_kwargs = {
                "host": self.config["server_ip"],
                "port": int(self.config.get("ssh_port", 22)),
                "username": self.config["ssh_username"],
                "known_hosts": None,
            }
            key_path = self.config.get("ssh_key_path")
            pwd = self.config.get("ssh_password")
            if key_path and os.path.isfile(key_path):
                conn_kwargs["client_keys"] = [key_path]
            elif pwd:
                conn_kwargs["password"] = pwd
            else:
                return None

            async with asyncssh.connect(**conn_kwargs) as conn:
                result = await conn.run(
                    'echo "$(hostname) | $(uptime) | $(free -h | grep Mem) | $(df -h / | tail -1)"'
                )
                return result.stdout.strip()
        except Exception as e:
            logger.error(f"SSH 连接失败: {e}")
            return None

    # ---------------- 发送消息 ----------------
    async def send_message(self, target: str, message: str):
        await self.context.send_message(target, [message])

    # ---------------- 定时每日报告 ----------------
    async def schedule_daily_report(self):
        while True:
            now = datetime.now()
            try:
                target_time = datetime.strptime(self.config["report_time"], "%H:%M").time()
            except ValueError:
                logger.error("report_time 格式错误，应为 HH:MM")
                await asyncio.sleep(3600)
                continue

            if now.time() >= target_time:
                await asyncio.sleep(86400 - (now.hour * 3600 + now.minute * 60 + now.second))
            else:
                wait_seconds = (target_time.hour * 3600 + target_time.minute * 60) - \
                               (now.hour * 3600 + now.minute * 60 + now.second)
                await asyncio.sleep(wait_seconds)

            await self._push_status(self.config["report_target"])

    # ---------------- 健康检查（断线报警） ----------------
    async def schedule_health_check(self):
        while True:
            await asyncio.sleep(300)  # 每 5 分钟
            status = await self.get_server_status()
            if status:
                self.connected = True
                self.last_success = datetime.now()
            else:
                if self.connected and self.last_success and \
                   (datetime.now() - self.last_success).total_seconds() > 1200:  # 20 分钟
                    await self.send_message(
                        self.config["alert_target"],
                        "🚨 服务器断联超过20分钟！"
                    )
                    self.connected = False

    # ---------------- 统一状态推送 ----------------
    async def _push_status(self, target: str):
        status = await self.get_server_status()
        if status:
            await self.send_message(target, f"📊 服务器状态报告：\n{status}")
        else:
            await self.send_message(target, "❌ 无法连接服务器")

    # ---------------- 持久化配置 ----------------
    def _save_config(self):
        cfg_path = os.path.join("data", "config", "server_monitor_config.json")
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 指令组 /server
    # ------------------------------------------------------------------
    @filter.command_group("server")
    def server_group(self):
        """服务器管理指令组"""
        pass

    # /server status
    @server_group.command("status")
    async def server_status(self, event: AstrMessageEvent):
        """查看服务器实时连接状态"""
        status = await self.get_server_status()
        if status:
            yield event.plain_result(f"✅ 服务器连接正常：\n{status}")
        else:
            yield event.plain_result("❌ 服务器连接失败，请检查 SSH 配置或网络状态")

    # /server push
    @server_group.command("push")
    async def server_push(self, event: AstrMessageEvent):
        """手动触发一次每日状态推送"""
        target = event.unified_msg_origin
        await self._push_status(target)

    # ------------------------------------------------------------------
    # 动态配置指令 /server set <key> <value>
    # ------------------------------------------------------------------
    @server_group.command("set")
    async def server_set(self, event: AstrMessageEvent, key: str, value: str):
        """/server set <key> <value>  动态修改配置项"""
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
            yield event.plain_result(
                "❌ 未知键值。支持：ip、port、user、pwd、key、report_time、report_target、alert_target"
            )
            return

        real_key = valid_keys[key]

        # 端口转 int
        if real_key == "ssh_port":
            try:
                value = int(value)
            except ValueError:
                yield event.plain_result("❌ 端口必须是整数")
                return

        # key 文本 → 临时文件
        if real_key == "ssh_key_path":
            fd, temp_path = tempfile.mkstemp(prefix="srv_key_", suffix=".pem", text=True)
            try:
                os.write(fd, value.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(temp_path, 0o600)
            value = temp_path
            # 清理旧的临时私钥文件
            old_path = self.config.get("ssh_key_path", "")
            if old_path.startswith(tempfile.gettempdir()):
                try:
                    os.remove(old_path)
                except Exception:
                    pass

        # 更新并持久化
        self.config[real_key] = value
        try:
            self._save_config()
            yield event.plain_result(f"✅ 已更新 {real_key}")
        except Exception as e:
            yield event.plain_result(f"❌ 保存配置失败：{e}")

    # /server show
    @server_group.command("show")
    async def server_show(self, event: AstrMessageEvent):
        """查看当前所有配置"""
        lines = ["📄 当前配置："]
        for k in ("server_ip", "ssh_port", "ssh_username", "ssh_password",
                  "ssh_key_path", "report_time", "report_target", "alert_target"):
            v = self.config.get(k, "未设置")
            if k == "ssh_password" and v:
                v = "***"
            lines.append(f"{k}: {v}")
        yield event.plain_result("\n".join(lines))
