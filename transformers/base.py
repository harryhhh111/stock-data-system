"""
transformers/base.py — 数据标准化基类
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class BaseTransformer(ABC):
    """数据标准化转换器基类。

    子类需实现 ``transform`` 方法，将原始 DataFrame 转换为标准字段列表。
    """

    @abstractmethod
    def transform(self, raw_df: pd.DataFrame, market: str = "CN_A") -> list[dict[str, Any]]:
        """将原始 DataFrame 转换为标准化记录列表。

        Args:
            raw_df: 原始 DataFrame（来自 fetcher 层）
            market: 市场标识，'CN_A' 或 'HK'

        Returns:
            标准化字典列表，每个字典对应一行报表记录
        """
        ...


if __name__ == "__main__":
    print("BaseTransformer 是抽象基类，不能直接实例化。")
    print("请使用 EastmoneyTransformer 或 EastmoneyHkTransformer。")
