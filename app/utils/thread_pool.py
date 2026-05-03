"""
全局共享线程池，用于将 CPU 密集/阻塞 I/O 任务卸载出 asyncio 事件循环。
2C 服务器配置：max_workers=2（1 核给事件循环，1 核做 CPU 计算）。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

# 全局共享线程池（应用生命周期内保持）
_cpu_pool = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="cpu_worker",
)


async def run_in_thread(func, *args, **kwargs):
    """
    将同步（CPU 密集或阻塞 I/O）函数卸载到线程池运行。
    保持 asyncio 事件循环畅通，SSE 推送不断流。

    用法：
        result = await run_in_thread(blocking_function, arg1, arg2)
    """
    loop = asyncio.get_running_loop()
    if kwargs:
        func = partial(func, **kwargs)
    return await loop.run_in_executor(_cpu_pool, func, *args)


def shutdown_pool():
    """应用关闭时优雅释放线程池（同步，由 _shutdown_hook 包裹调用）"""
    _cpu_pool.shutdown(wait=True)
