# -*- coding: utf-8 -*-
"""
日志工具模块
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional


class DailyRotatingFileHandler(TimedRotatingFileHandler):
    """
    按日期轮转的日志处理器
    
    首次启动即使用带日期的文件名格式：blivepost.2026-03-09.log
    """
    
    def __init__(self, filename, when='midnight', interval=1, backupCount=0,
                 encoding=None, delay=False, utc=False):
        # 生成带日期的初始文件名
        self.baseFilename = str(filename)
        self.date_suffix = datetime.now().strftime("%Y-%m-%d")
        self.current_file = f"{self.baseFilename}.{self.date_suffix}.log"
        
        super().__init__(
            self.current_file, when=when, interval=interval,
            backupCount=backupCount, encoding=encoding, delay=delay, utc=utc
        )
        
        # 保存原始baseFilename用于轮转
        self._baseFilename = self.baseFilename
    
    def shouldRollover(self, record):
        """
        判断是否需要轮转
        """
        # 检查日期是否变化
        current_suffix = datetime.now().strftime("%Y-%m-%d")
        if current_suffix != self.date_suffix:
            return True
        return False
    
    def doRollover(self):
        """
        执行日志轮转
        """
        if self.stream:
            self.stream.close()
            self.stream = None
        
        # 更新日期后缀
        self.date_suffix = datetime.now().strftime("%Y-%m-%d")
        self.baseFilename = f"{self._baseFilename}.{self.date_suffix}.log"
        
        # 删除旧日志（超过backupCount）
        if self.backupCount > 0:
            dir_name, base_name = os.path.split(self._baseFilename)
            file_names = os.listdir(dir_name) if os.path.exists(dir_name) else []
            log_files = sorted([
                f for f in file_names 
                if f.startswith(base_name + ".") and f.endswith(".log")
            ])
            if len(log_files) > self.backupCount:
                for f in log_files[:len(log_files) - self.backupCount]:
                    os.remove(os.path.join(dir_name, f))
        
        # 创建新文件
        self.stream = open(self.baseFilename, 'a', encoding=self.encoding)


class Logger:
    """应用日志管理器"""
    
    _instance: Optional["Logger"] = None
    _logger: Optional[logging.Logger] = None
    
    def __new__(cls) -> "Logger":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def setup(
        self,
        name: str = "blivepost",
        level: int = logging.INFO,
        log_dir: Optional[str] = None,
        console: bool = True,
    ) -> logging.Logger:
        """
        设置日志系统
        
        Args:
            name: 日志名称
            level: 日志级别
            log_dir: 日志文件目录
            console: 是否输出到控制台
            
        Returns:
            logging.Logger: 配置好的日志器
        """
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        
        # 清除现有处理器
        self._logger.handlers.clear()
        
        # 日志格式
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        
        # 控制台处理器
        if console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(level)
            console_handler.setFormatter(formatter)
            self._logger.addHandler(console_handler)
        
        # 文件处理器（支持按日期轮转）
        if log_dir:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)
            
            # 使用自定义的DailyRotatingFileHandler
            # 首次启动即生成带日期的文件名：blivepost.2026-03-09.log
            log_file = log_path / "blivepost"
            file_handler = DailyRotatingFileHandler(
                filename=log_file,
                when="midnight",      # 每天午夜轮转
                backupCount=30,       # 保留30天日志
                encoding="utf-8",
                utc=False             # 使用本地时间
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            self._logger.addHandler(file_handler)
        
        return self._logger
    
    def get_logger(self) -> logging.Logger:
        """获取日志器"""
        if self._logger is None:
            return self.setup()
        return self._logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    获取日志器
    
    Args:
        name: 模块名称（可选）
        
    Returns:
        logging.Logger: 日志器实例
    """
    logger = Logger().get_logger()
    if name:
        return logging.getLogger(f"blivepost.{name}")
    return logger

