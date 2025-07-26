# main.py
import asyncio
import asyncssh
import json
import os
import tempfile
import subprocess
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register(
    "server_monitor",
    "imRhoAias",
    "远程服务器监控与报警插件（增强版 + 缓存 + 复用 + 失败详情）",
    "1.0.4",
    "https://github.com/imRhoAias/astrbot_plugin_server_monitor"
)
class ServerMonitorPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.last_success: Optional[datetime] = None
        self.connected = False
        self._reconnect_lock = asyncio.Lock()
        self._current_config_digest = self._digest_config()

        # 1. 本地 60 秒缓存
        self.cache_path = os.path.join("data", "cache", "server_monitor_cache.json")
        self.cache: Dict[str, Dict[str, Any]] = {}
        self._load_cache()

        # 2. SSH 连接复用
        self._ssh: Optional[asyncssh.SSHClientConnection] = None

        asyncio.create_task(self.start_monitoring())

    # ---------- 工具 ----------
    def _digest_config(self) -> str:
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

    def _load_cache(self):
        if os.path.isfile(self.cache_path):
            try:
                with open(self.cache_path, encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                pass

    def _save_cache(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, indent=2, ensure_ascii=False)

    async def send_message(self, target: str, message: str):
        await self.context.send_message(target, [message])

    # ---------- 配置变动后触发 ----------
    async def _on_config_changed(self, event: AstrMessageEvent | None = None):
        async with self._reconnect_lock:
            ip = self.config.get("server_ip")
            if not ip:
                return
            ping_ok, rtt = await self._ping(ip)
            if event:
                await event.send(event.plain_result(
                    f"🛜 ping {ip}: " + (f"{rtt:.1f} ms" if ping_ok else "超时/丢包")
                ))
            if not ping_ok:
                if event:
                    await event.send(event.plain_result("❌ 网络不可达，终止 SSH 连接尝试"))
                return
            ok, msg = await self._connect_with_retry()
            if event:
                await event.send(event.plain_result(msg))

    # ---------- ping ----------
    async def _ping(self, host: str) -> tuple[bool, float]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "3", "-W", "1", host,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            for line in stdout.decode().splitlines():
                if "avg" in line or "average" in line:
                    try:
                        return True, float(line.split("/")[4])
                    except Exception:
                        pass
            return False, 0.0
        except Exception:
            return False, 0.0

    # ---------- 带策略 SSH ----------
    async def _connect_with_retry(self) -> tuple[bool, str]:
        attempts = 0
        start = datetime.now()
        while attempts < 4 and (datetime.now() - start) < timedelta(seconds=120):
            try:
                await self._ssh_conn()
                self.connected = True
                self.last_success = datetime.now()
                return True, "✅ SSH 连接成功"
            except asyncssh.PermissionDenied:
                self.connected = False
                return False, "❌ 认证失败：用户名/密码或密钥错误"
            except asyncssh.TimeoutError:
                attempts += 1
                logger.warning(f"SSH 第 {attempts}/4 次超时")
                await asyncio.sleep(15)
            except OSError as e:
                attempts += 1
                logger.warning(f"SSH 第 {attempts}/4 次网络不可达: {e}")
                await asyncio.sleep(15)
            except Exception as e:
                attempts += 1
                logger.warning(f"SSH 第 {attempts}/4 次失败: {e}")
                await asyncio.sleep(15)

        self.connected = False
        return False, "❌ 连续 4 次连接失败，请检查配置"

    # ---------- SSH 连接复用 ----------
    async def _ssh_conn(self) -> asyncssh.SSHClientConnection:
        if self._ssh and not self._ssh.is_closing():
            return self._ssh

        conn_kwargs = {
            "host": self.config["server_ip"],
            "port": int(self.config.get("ssh_port", 22)),
            "username": self.config["ssh_username"],
            "known_hosts": None,
            "config": ["StrictHostKeyChecking=no"],
            "connect_timeout": 10,
            "keepalive_interval": 30
        }
        pwd = self.config.get("ssh_password")
        key = self.config.get("ssh_key_path")
        if key and os.path.isfile(key):
            conn_kwargs["client_keys"] = [key]
        elif pwd:
            conn_kwargs["password"] = pwd

        self._ssh = await asyncssh.connect(**conn_kwargs)
        return self._ssh

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

    # ---------- 带缓存的状态采集 ----------
    async def get_server_status(self) -> str | None:
        ip = self.config["server_ip"]
        now = datetime.now().timestamp()

        # 读缓存
        if ip in self.cache and (now - self.cache[ip]["ts"]) < 60:
            return self.cache[ip]["status"]

        fresh = await self._fetch_status()
        if fresh:
            self.cache[ip] = {"ts": now, "status": fresh}
            self._save_cache()
        return fresh

    async def _fetch_status(self) -> str | None:
        try:
            async with self._ssh_conn() as conn:
                cmd = r"""hostname && \
uptime -p | sed 's/up //' && \
awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2} END {printf "%d", (t-a)/t*100}' /proc/meminfo && \
df -h / | awk 'NR==2 {print $5}' | tr -d '%'"""
                result = await conn.run(cmd)
                lines = result.stdout.strip().splitlines()
                if len(lines) >= 4:
                    host, uptime, mem, disk = lines
                    return f"📌 {host} | ⏱ {uptime} | 💾 内存 {mem}% | 💿 磁盘 {disk}%"
        except Exception:
            pass
        return None

    async def _push_status(self, target: str):
        status = await self.get_server_status()
        if status:
            await self.send_message(target, f"📊 服务器状态：\n{status}")
        else:
            await self.send_message(target, "❌ 无法连接服务器")

    # ---------- 指令组 ----------
    @filter.command_group("server")
    def server_group(self):
        """服务器管理指令组"""
        pass

    @server_group.command("status")
    async def server_status(self, event: AstrMessageEvent):
        status = await self.get_server_status()
        if status:
            yield event.plain_result(f"✅ {status}")
        else:
            yield event.plain_result("❌ 服务器连接失败")

    @server_group.command("push")
    async def server_push(self, event: AstrMessageEvent):
        await self._push_status(event.unified_msg_origin)

    @server_group.command("set")
    async def server_set(self, event: AstrMessageEvent, key: str, value: str):
        key = key.lower()
        valid_keys = {
            "ip": "server_ip", "port": "ssh_port", "user": "ssh_username",
            "pwd": "ssh_password", "key": "ssh_key_path",
            "report_time": "report_time",
            "report_target": "report_target", "alert_target": "alert_target"
        }
        if key not in valid_keys:
            yield event.plain_result("❌ 未知键值")
            return

        real_key = valid_keys[key]
        if real_key == "ssh_port":
            try:
                value = int(value)
            except ValueError:
                yield event.plain_result("❌ 端口必须是整数")
                return

        if real_key == "ssh_key_path":
            fd, temp_path = tempfile.mkstemp(prefix="srv_key_", suffix=".pem", text=True)
            try:
                os.write(fd, value.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(temp_path, 0o600)
            value = temp_path
            old = self.config.get("ssh_key_path", "")
            if old.startswith(tempfile.gettempdir()):
                try:
                    os.remove(old)
                except Exception:
                    pass

        old_digest = self._current_config_digest
        self.config[real_key] = value
        self._save_config()
        yield event.plain_result(f"✅ 已更新 {real_key}")
        if self._digest_config() != old_digest:
            asyncio.create_task(self._on_config_changed(event))

    @server_group.command("show")
    async def server_show(self, event: AstrMessageEvent):
        lines = ["📄 当前配置："]
        for k in ("server_ip", "ssh_port", "ssh_username", "ssh_password",
                  "ssh_key_path", "report_time", "report_target", "alert_target"):
            v = self.config.get(k, "未设置")
            if k == "ssh_password" and v:
                v = "***"
            lines.append(f"{k}: {v}")
        yield event.plain_result("\n".join(lines))
