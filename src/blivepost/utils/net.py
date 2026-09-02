# -*- coding: utf-8 -*-
"""网络快速探测工具"""
import socket


def tcp_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    """
    快速TCP可达性探测（Redis等本地服务的短超时预检）

    Windows上redis-py的socket_connect_timeout对双栈localhost可能
    不严格执行（串行尝试各地址族导致数十秒等待），此探测以明确
    的短超时先行判断，不通则直接跳过Redis操作。

    Args:
        host: 主机名或IP
        port: 端口
        timeout: 单地址连接超时秒数

    Returns:
        bool: 端口是否可达
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
