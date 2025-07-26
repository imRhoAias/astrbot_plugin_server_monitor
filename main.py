import asyncio
import asyncssh
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

@register("server_monitor", "nalanox", "远程服务器监控与报警插件", "1.0.0", "https://github.com/imRhoAias/astrbot_plugin_server_monitor")
class ServerMonitorPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.last_success = None
        self.connected = False
        asyncio.create_task(self.start_monitoring())

    async def start_monitoring(self):
        """启动定时任务"""
        await self.schedule_daily_report()
        await self.schedule_health_check()

    async def get_server_status(self):
        """通过 SSH 获取服务器状态"""
        try:
            async with asyncssh.connect(
                self.config['server_ip'],
                port=self.config.get('ssh_port', 22),
                username=self.config['ssh_username'],
                password=self.config.get('ssh_password'),
                client_keys=[self.config.get('ssh_key_path')] if self.config.get('ssh_key_path') else None,
                known_hosts=None
            ) as conn:
                result = await conn.run('echo "$(hostname) | $(uptime) | $(free -h | grep Mem) | $(df -h / | tail -1)"')
                return result.stdout.strip()
        except Exception as e:
            logger.error(f"SSH 连接失败: {e}")
            return None

    async def send_message(self, target: str, message: str):
        """发送消息到指定目标"""
        await self.context.send_message(target, [message])

    async def schedule_daily_report(self):
        """每天固定时间发送状态报告"""
        while True:
            now = datetime.now()
            target_time = datetime.strptime(self.config['report_time'], "%H:%M").time()
            if now.time() >= target_time:
                await asyncio.sleep(86400 - (now.hour * 3600 + now.minute * 60 + now.second))
            else:
                wait_seconds = (target_time.hour * 3600 + target_time.minute * 60) - (now.hour * 3600 + now.minute * 60 + now.second)
                await asyncio.sleep(wait_seconds)
            status = await self.get_server_status()
            if status:
                await self.send_message(self.config['report_target'], f"📊 服务器状态报告：\n{status}")
            else:
                await self.send_message(self.config['report_target'], "❌ 无法连接服务器")

    async def schedule_health_check(self):
        """每 5 分钟检查一次服务器状态"""
        while True:
            await asyncio.sleep(300)  # 5分钟
            status = await self.get_server_status()
            if status:
                self.connected = True
                self.last_success = datetime.now()
            else:
                if self.connected and (datetime.now() - self.last_success).total_seconds() > 1200:  # 20分钟
                    await self.send_message(self.config['alert_target'], "🚨 服务器断联超过20分钟！")
                    self.connected = False
