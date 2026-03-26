import pytest
from unittest.mock import patch, MagicMock


class TestFilterColumns:
    """_filter_columns 列过滤逻辑测试（mock DB）。"""

    def test_all_columns_exist(self):
        """所有列都存在时，不做过滤。"""
        with patch("db.Connection") as mock_conn_cls:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = [("stock_code",), ("stock_name",), ("market",)]
            mock_conn_cls.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)

            # 清除缓存确保重新查询
            import db
            db._table_columns_cache = {}

            from db import _filter_columns
            result = _filter_columns("test_table", ["stock_code", "stock_name"])
            assert sorted(result) == ["stock_code", "stock_name"]

    def test_filters_nonexistent_columns(self):
        """不存在的列被过滤掉。"""
        with patch("db.Connection") as mock_conn_cls:
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = [("stock_code",), ("market",)]
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
            mock_conn_cls.return_value = mock_conn

            import db
            db._table_columns_cache = {}

            from db import _filter_columns
            result = _filter_columns("test_table", ["stock_code", "nonexistent", "market"])
            assert sorted(result) == ["market", "stock_code"]

    def test_empty_result_when_all_filtered(self):
        """所有列都不存在时返回空列表。"""
        with patch("db.Connection") as mock_conn_cls:
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = [("id",)]
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
            mock_conn_cls.return_value = mock_conn

            import db
            db._table_columns_cache = {}

            from db import _filter_columns
            result = _filter_columns("test_table", ["col_a", "col_b"])
            assert result == []

    def test_warning_logged_on_filtered_columns(self, caplog):
        """列被过滤时输出 WARNING 级别日志。"""
        import logging
        with caplog.at_level(logging.WARNING, logger="db"):
            with patch("db.Connection") as mock_conn_cls:
                mock_cur = MagicMock()
                mock_cur.fetchall.return_value = [("stock_code",)]
                mock_conn = MagicMock()
                mock_conn.__enter__ = MagicMock(return_value=mock_conn)
                mock_conn.__exit__ = MagicMock(return_value=False)
                mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
                mock_conn_cls.return_value = mock_conn

                import db
                db._table_columns_cache = {}

                from db import _filter_columns
                _filter_columns("test_table", ["stock_code", "bad_column"])

                # 验证有 WARNING 日志
                warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
                assert len(warning_records) >= 1, "应输出 WARNING 级别日志"
                assert "bad_column" in caplog.text or "1 列被过滤" in caplog.text
