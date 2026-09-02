# -*- coding: utf-8 -*-
"""
指纹服务 - B站WBI签名和会话管理
"""

import random
import time
from typing import Optional, Tuple

import requests

from ..config.settings import Settings, RedisConfig
from ..utils.logger import get_logger
from .wbi import WbiSigner, wbi_sign


logger = get_logger("fingerprint")

# 请求头
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
}


class CacheBackend:
    """缓存后端接口"""
    
    def get(self, key: str) -> Optional[str]:
        raise NotImplementedError
    
    def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        raise NotImplementedError


class MemoryCache(CacheBackend):
    """内存缓存实现"""
    
    def __init__(self):
        self._cache: dict = {}
        self._expire: dict = {}
    
    def get(self, key: str) -> Optional[str]:
        if key in self._expire and time.time() > self._expire[key]:
            del self._cache[key]
            del self._expire[key]
            return None
        return self._cache.get(key)
    
    def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        self._cache[key] = value
        if ex:
            self._expire[key] = time.time() + ex


class RedisCache(CacheBackend):
    """Redis缓存实现"""

    # 进程级降级记忆：连接失败过一次后，本进程内不再尝试Redis
    _connect_failed = False

    def __init__(self, config: RedisConfig):
        self._redis = None
        try:
            # TCP预检：不可达直接降级内存缓存（Windows上redis连接超时不可靠）
            from ..utils.net import tcp_reachable
            if not tcp_reachable(config.host, config.port, timeout=0.5):
                raise RuntimeError("Redis端口不可达")
            import redis
            if RedisCache._connect_failed:
                raise RuntimeError("Redis此前连接失败（进程内降级）")
            self._redis = redis.Redis(
                host=config.host,
                port=config.port,
                password=config.password if config.password else None,
                db=config.db,
                decode_responses=True,
                # 短超时：Redis不可达时快速降级内存缓存，避免拖慢启动
                socket_connect_timeout=1,
                socket_timeout=1,
                retry_on_timeout=False,
            )
            # 测试连接
            self._redis.ping()
            logger.info("Redis连接成功")
        except Exception as e:
            RedisCache._connect_failed = True
            logger.warning(f"Redis连接失败，使用内存缓存: {e}")
            self._redis = None
    
    @property
    def available(self) -> bool:
        return self._redis is not None
    
    def get(self, key: str) -> Optional[str]:
        if not self._redis:
            return None
        try:
            result = self._redis.get(key)
            return str(result) if result is not None else None
        except Exception:
            return None
    
    def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        if not self._redis:
            return
        try:
            self._redis.set(key, value, ex=ex)
        except Exception:
            pass


class FingerprintService:
    """
    指纹服务
    
    负责B站WBI签名、会话管理、指纹生成等功能
    """
    
    def __init__(self, settings: Settings, sessdata: str = ""):
        """
        初始化指纹服务
        
        Args:
            settings: 配置对象
            sessdata: B站登录凭证
        """
        self.settings = settings
        self._sessdata = sessdata

        # 初始化缓存
        self._cache: CacheBackend = self._init_cache()

        # WBI签名器（密钥获取统一走wbi模块，本地仅做缓存）
        self._wbi = WbiSigner()
        self._w_key: Optional[str] = None
        self._w_key_expire: float = 0
    
    def _init_cache(self) -> CacheBackend:
        """初始化缓存后端"""
        try:
            redis_cache = RedisCache(self.settings.redis)
            if redis_cache.available:
                return redis_cache
        except Exception:
            pass
        return MemoryCache()
    
    @property
    def sessdata(self) -> str:
        """获取SESSDATA（仅保存在进程内存，不落共享缓存）"""
        if not self._sessdata:
            logger.warning("未设置SESSDATA，部分功能可能受限")
        return self._sessdata

    @sessdata.setter
    def sessdata(self, value: str) -> None:
        """设置SESSDATA（仅进程内存，避免凭证写入Redis等共享存储）"""
        self._sessdata = value or ""
    
    def set_sessdata(self, sessdata: str) -> None:
        """设置SESSDATA（兼容旧接口）"""
        self.sessdata = sessdata
    
    def get_buvid(self) -> str:
        """
        获取buvid信息
        
        Returns:
            str: buvid cookie字符串
        """
        buvid3 = self._cache.get("buvid3")
        buvid4 = self._cache.get("buvid4")
        
        if not buvid3 or not buvid4:
            try:
                buvid_url = "https://api.bilibili.com/x/frontend/finger/spi"
                response = requests.get(url=buvid_url, headers=HEADERS, timeout=10)
                buvid_obj = response.json()
                buvid3 = buvid_obj["data"]["b_3"]
                buvid4 = buvid_obj["data"]["b_4"]
                
                expire_time = 86400 * 365 - int(time.time() + 8 * 3600) % 86400
                self._cache.set("buvid3", buvid3, ex=expire_time)
                self._cache.set("buvid4", buvid4, ex=expire_time)
            except Exception as e:
                logger.warning(f"获取buvid失败: {e}")
                return ""
        
        return f"buvid3={buvid3};buvid4={buvid4}"
    
    def get_fingerprint(self) -> Tuple[str, str]:
        """
        获取指纹信息
        
        Returns:
            Tuple[str, str]: (fingerprint, buvid_fp)
        """
        fingerprint = self._cache.get("fingerprint")
        buvid_fp = self._cache.get("buvid_fp")
        
        if not fingerprint or not buvid_fp:
            # 生成随机指纹
            fingerprint = hex(random.randrange(0, 16 ** 32))[2:].upper()
            fingerprint = ("0" * (32 - len(fingerprint)) + fingerprint).lower()
            
            expire_time = 86400 * 365 - int(time.time() + 8 * 3600) % 86400
            self._cache.set("fingerprint", fingerprint, ex=expire_time)
            
            # 获取buvid_fp
            try:
                fp_headers = {
                    "cookie": self.get_buvid(),
                    "user-agent": HEADERS["user-agent"]
                }
                fp_fmc_url = f"https://api.bilibili.com/x/frontend/finger/fpfmc?fp={fingerprint}"
                response = requests.get(url=fp_fmc_url, headers=fp_headers, timeout=10)
                fp_fmc_obj = response.json()
                buvid_fp = fp_fmc_obj["data"]["buvid_fp"]
                self._cache.set("buvid_fp", buvid_fp, ex=expire_time)
            except Exception as e:
                logger.warning(f"获取buvid_fp失败: {e}")
                buvid_fp = ""
        
        return fingerprint, buvid_fp
    
    def get_sid(self) -> str:
        """
        获取sid
        
        Returns:
            str: sid值
        """
        sid = self._cache.get("sid")
        
        if not sid:
            try:
                sid_url = "https://api.bilibili.com/x/player/wbi/v2?aid=114980603102689"
                response = requests.get(url=sid_url, headers=HEADERS, timeout=10)
                parts = response.headers.get("Set-Cookie", "").split("sid=")
                if len(parts) > 1:
                    sid = parts[1].split(";")[0]
                else:
                    sid = ""
                
                if sid:
                    expire_time = 86400 * 365 - int(time.time() + 8 * 3600) % 86400
                    self._cache.set("sid", sid, ex=expire_time)
            except Exception as e:
                logger.warning(f"获取sid失败: {e}")
                sid = ""
        
        return sid
    
    def get_cookie(self) -> str:
        """
        获取完整cookie
        
        Returns:
            str: 完整的cookie字符串
        """
        fp, buvidfp = self.get_fingerprint()
        buvid = self.get_buvid()
        sid = self.get_sid()
        sessdata = self.sessdata
        
        parts = []
        if buvid:
            parts.append(buvid)
        if fp:
            parts.append(f"fingerprint={fp}")
        if buvidfp:
            parts.append(f"buvid_fp={buvidfp}")
            parts.append("buvid_fp_plain=undefined")
        if sid:
            parts.append(f"sid={sid}")
        if sessdata:
            parts.append(f"SESSDATA={sessdata}")
        
        return ";".join(parts)

    async def get_cookie_async(self) -> str:
        """
        异步获取完整cookie（同步请求在线程中执行，不阻塞事件循环）

        Returns:
            str: 完整的cookie字符串
        """
        import asyncio
        return await asyncio.to_thread(self.get_cookie)

    def get_w_key(self, force_refresh: bool = False) -> str:
        """
        获取WBI密钥（内存/Redis缓存 + wbi模块获取）

        Args:
            force_refresh: 是否强制刷新

        Returns:
            str: WBI密钥
        """
        now = time.time()

        if not force_refresh and self._w_key and now < self._w_key_expire:
            return self._w_key

        # 尝试从缓存获取
        cached_key = self._cache.get("w_key")
        if not force_refresh and cached_key:
            self._w_key = cached_key
            self._w_key_expire = now + 86400
            return self._w_key

        key = self._wbi.get_w_key(force_refresh=True)
        if key:
            self._w_key = key
            self._w_key_expire = now + 86400
            self._cache.set("w_key", key, ex=86400)
        return key

    def sign_wbi(self, params: str, wts: Optional[str] = None) -> str:
        """
        生成WBI签名

        Args:
            params: 参数字符串
            wts: 时间戳，默认使用当前时间

        Returns:
            str: 签名
        """
        if wts is None:
            wts = str(int(time.time()))
        return wbi_sign(params, self.get_w_key(), wts)
