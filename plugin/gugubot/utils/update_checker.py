import logging
from typing import Optional

import requests
from mcdreforged.api.types import PluginServerInterface
from packaging import version

# 版本检查的网络超时（连接, 读取）。
#
# **必须有 timeout**：``requests`` 默认无超时——服务端「接受连接但永不
# 响应」时会永久挂住。这个检查在插件加载时跑，一旦挂死就是线程永久
# 泄漏。
_TIMEOUT = (5, 10)

# 上游仓库（与本插件 mcdreforged.plugin.json 的 link 一致）
_LATEST_RELEASE_API = (
    "https://api.github.com/repos/PFingan-Code/PF-GUGUBot/releases/latest"
)


def _fetch_latest_version(logger: Optional[logging.Logger] = None) -> Optional[str]:
    """查询上游最新版本号（纯 HTTP，返回 ``tag_name`` 去掉 ``v``；失败 None）。

    **必须是普通函数、不碰 server 对象**——它会被丢进线程池执行。
    协程里直接调它（或直接调 ``requests.get``）会阻塞 MCDR 事件循环
    （见 #75）。
    """
    try:
        response = requests.get(_LATEST_RELEASE_API, timeout=_TIMEOUT)
    except Exception as e:
        if logger:
            logger.debug(f"版本检查请求失败: {e}")
        return None

    if response.status_code != 200:
        if logger:
            logger.warning(f"无法检查插件版本，网络代码: {response.status_code}")
        return None

    try:
        return str(response.json()["tag_name"]).replace("v", "")
    except Exception as e:
        if logger:
            logger.debug(f"版本检查响应解析失败: {e}")
        return None


# 检查插件版本
def check_plugin_version(server: PluginServerInterface):
    """返回一个协程函数，供 ``server.schedule_task`` 使用。

    旧实现把 **同步** 的 ``requests.get`` 直接写在协程体里，且**没有
    timeout** —— MCDR 用 async executor ``await`` 这个协程，于是整个
    请求期间**事件循环完全停摆**：服务器命令、QQ 消息、定时任务全部
    排队；若对端不响应则**永久冻结**（日志里只有一行「检查插件版本时
    出错」，而且得等到它真抛异常）。

    现在：HTTP 抽成纯函数 ``_fetch_latest_version``，用
    ``run_in_executor`` 丢线程池，事件循环始终可调度；请求本身带
    timeout，最坏情况也只会占用一个线程池线程 10 秒。
    """

    async def check_plugin_version_task():
        import asyncio

        loop = asyncio.get_running_loop()
        # 关键：放到线程池里跑，绝不阻塞事件循环
        latest_version = await loop.run_in_executor(
            None, _fetch_latest_version, server.logger
        )
        if not latest_version:
            return

        try:
            current_version = str(server.get_self_metadata().version)
            if version.parse(latest_version) > version.parse(current_version):
                server.logger.info(
                    f"§e[PF-GUGUBot] §6有新版本可用: §b{latest_version}§6，当前版本: §b{current_version}"
                )
                server.logger.info(
                    "§e[PF-GUGUBot] §6请使用 §b!!MCDR plugin install -U -y gugubot §6来更新插件"
                )
            else:
                server.logger.info(
                    f"§e[PF-GUGUBot] §6已是最新版本: §b{current_version}"
                )
        except Exception as e:
            server.logger.warning(f"检查插件版本时出错: {e}")

    return check_plugin_version_task
