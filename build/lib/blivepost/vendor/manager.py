# -*- coding: utf-8 -*-
"""
Vendor管理器 - 管理第三方库的下载和更新
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from ..utils.logger import get_logger


logger = get_logger("vendor")

# 配置常量
CHECK_INTERVAL_HOURS = 24
DOWNLOAD_TIMEOUT = 12
CHUNK_SIZE = 1024 * 256
MAX_WORKERS = 3

# 下载回退链：codeload与github官方源在前（海外快），镜像在后（国内兜底）。
# 注：jsdelivr的gh仓库zip归档已废弃，不再列入。
CDN_TEMPLATES = [
    "https://codeload.github.com/{repo}/zip/refs/heads/{branch}",
    "https://github.com/{repo}/archive/refs/heads/{branch}.zip",
    "https://gh-proxy.com/https://github.com/{repo}/archive/refs/heads/{branch}.zip",
    "https://ghproxy.net/https://github.com/{repo}/archive/refs/heads/{branch}.zip",
]


class FileLock:
    """文件锁，用于进程间同步"""
    
    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self):
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                break
            except FileExistsError:
                time.sleep(0.2)

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.path)
        except OSError:
            pass


@dataclass
class VendorSpec:
    """Vendor规格定义"""
    name: str
    repo: str
    branch: str
    subdir: str
    sha256: Optional[str] = None
    builtin: bool = True
    background_update: bool = True


class VendorManager:
    """
    Vendor管理器
    
    负责第三方库的下载、更新和版本管理。
    支持从GitHub自动下载库并注入到Python路径中。
    """
    
    def __init__(self, app_name: str = ".vendor_cache"):
        """
        初始化Vendor管理器
        
        Args:
            app_name: 缓存目录名称
        """
        self.base_dir = self._get_base_dir(app_name)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(MAX_WORKERS)
        self._injected = False

    def _get_base_dir(self, app_name: str) -> Path:
        """获取基础目录"""
        if getattr(sys, "frozen", False):
            return Path.home() / app_name
        return Path(__file__).parent.parent.parent.parent / "_vendor"

    def _inject_path(self) -> None:
        """将vendor目录注入到sys.path"""
        if not self._injected and str(self.base_dir) not in sys.path:
            sys.path.insert(0, str(self.base_dir))
            self._injected = True

    def ensure_many(self, specs: list[VendorSpec]) -> None:
        """
        确保多个vendor都已安装
        
        Args:
            specs: Vendor规格列表
        """
        self._inject_path()
        for spec in specs:
            self.ensure(spec)

    def ensure(self, spec: VendorSpec) -> None:
        """
        确保指定的vendor已安装
        
        Args:
            spec: Vendor规格
        """
        self._inject_path()
        
        pkg_dir = self.base_dir / spec.name
        meta_path = self.base_dir / f"{spec.name}.json"
        meta = self._load_meta(meta_path)

        # 如果有内置版本，先安装
        if spec.builtin and not pkg_dir.exists():
            self._install_builtin(spec)

        # 检查是否需要更新
        if pkg_dir.exists():
            if self._needs_update(meta) and spec.background_update:
                self.executor.submit(self._safe_update, spec, meta_path)
            return

        # 不存在则下载
        self._safe_update(spec, meta_path)

    def _install_builtin(self, spec: VendorSpec) -> None:
        """安装内置版本"""
        # 检查_vendor目录下是否有内置库
        builtin_path = self.base_dir.parent / "_vendor" / spec.name
        if builtin_path.exists():
            pkg_dir = self.base_dir / spec.name
            if not pkg_dir.exists():
                shutil.copytree(builtin_path, pkg_dir)
                logger.info(f"已安装内置版本: {spec.name}")

    def _safe_update(self, spec: VendorSpec, meta_path: Path) -> None:
        """安全更新"""
        lock_path = self.base_dir / f"{spec.name}.lock"

        with FileLock(lock_path):
            try:
                self._update(spec, meta_path)
                logger.info(f"{spec.name} 更新完成")
            except Exception as e:
                logger.warning(f"{spec.name} 更新失败: {e}")

    def _update(self, spec: VendorSpec, meta_path: Path) -> None:
        """执行更新"""
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "repo.zip"
            self._download_fastest(spec, zip_path)

            if spec.sha256:
                self._verify_hash(zip_path, spec.sha256)

            src_dir = self._extract(zip_path, tmp, spec)
            self._atomic_replace(spec, src_dir)
            self._save_meta(meta_path)

    def _download_fastest(self, spec: VendorSpec, zip_path: Path) -> None:
        """从最快的CDN下载"""
        last_err: Optional[Exception] = None

        for url in [
            t.format(repo=spec.repo, branch=spec.branch)
            for t in CDN_TEMPLATES
        ]:
            try:
                with requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True) as r:
                    r.raise_for_status()
                    with open(zip_path, "wb") as f:
                        for chunk in r.iter_content(CHUNK_SIZE):
                            if chunk:
                                f.write(chunk)
                return
            except Exception as e:
                last_err = e

        raise RuntimeError(f"下载失败: {last_err}")

    def _extract(self, zip_path: Path, tmp: str, spec: VendorSpec) -> Path:
        """解压下载的文件"""
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)

        root = Path(tmp) / f"{spec.repo.split('/')[-1]}-{spec.branch}"
        return root / spec.subdir

    def _atomic_replace(self, spec: VendorSpec, src_dir: Path) -> None:
        """原子替换目录"""
        final_dir = self.base_dir / spec.name
        tmp_dir = self.base_dir / f"{spec.name}_new"

        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)

        shutil.copytree(src_dir, tmp_dir)

        if final_dir.exists():
            shutil.rmtree(final_dir)

        tmp_dir.rename(final_dir)

    def _verify_hash(self, file_path: Path, expected: str) -> None:
        """验证文件哈希"""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest().lower() != expected.lower():
            raise RuntimeError("SHA256校验失败")

    def _load_meta(self, path: Path) -> dict:
        """加载元数据"""
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _save_meta(self, path: Path) -> None:
        """保存元数据"""
        path.write_text(json.dumps({"last_check": time.time()}))

    def _needs_update(self, meta: dict) -> bool:
        """检查是否需要更新"""
        if not meta:
            return True
        return (time.time() - meta.get("last_check", 0)) > (CHECK_INTERVAL_HOURS * 3600)


def setup_blivedm() -> None:
    """
    设置blivedm库
    
    确保blivedm库已安装并可用
    """
    vm = VendorManager(app_name="blivepost_vendor")
    vm.ensure(
        VendorSpec(
            name="blivedm",
            repo="xfgryujk/blivedm",
            branch="master",
            subdir="blivedm",
            builtin=True,
            background_update=True
        )
    )
