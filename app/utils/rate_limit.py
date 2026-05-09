"""
全局 SlowAPI 限流器（单独模块，避免循环导入）。

main.py 和各 router 均从此处导入 limiter。
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

# 基于真实客户端 IP（Nginx 反代时需设置 X-Forwarded-For）
limiter = Limiter(key_func=get_remote_address)
