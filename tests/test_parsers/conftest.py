"""裁切单页 PDF fixture：从 /tmp/hkex_samples 调研样本中裁出现金流量表页（≤300KB）。

样本不在本机时整体 skip（不发外网）。裁切结果缓存到 /tmp/hk_capex_test_fixtures/，
重复跑测试不重复裁切。
"""
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="需要 pymupdf")

SAMPLES = Path("/tmp/hkex_samples")
FIX_CACHE = Path("/tmp/hk_capex_test_fixtures")

# (fixture 名, 样本文件名, 页内定位词, 期望页内包含的补充词或 None)
CASES = {
    # 基准：安踏 2026H1，已付資本性開支 -1453/-1223 RMB 百万，上年經重列
    "anta_cf": ("02020_2026H1_2026082600214_c.pdf", "已付資本性開支", "經重列"),
    # 单位陷阱：港交所表头"以港元為單位"但实为百万；总部物业行须排除
    "hkex_cf": ("00388_2026H1_2026081900465_c.pdf", "購置固定資產及無形資產所支付款項", "投資活動之現金流量"),
    # 双币种：长和 美元列在前、港币报告列在后
    "ckh_cf": ("00001_2026H1_2026081300223_c.pdf", "購入固定資產", "百萬美元"),
    # 角注：友邦 無形資產付款角注13；PPE+投资物业合并付款行为目标
    "aia_cf": ("01299_2026H1_2026082000002_c.pdf", "投資物業以及物業、廠房及設備付款", "百萬美元"),
    # 6月财年：江南布衣 列=H1FY26（止2025-12-31 六個月）
    "jnby_cf": ("03306_2026H1_2026022600805_c.pdf", "購買不動產、廠房及設備", "六個月"),
    # MD&A 季度资本开支表（止三個月）：期间校验必须拒止
    "xiaomi_md": ("01810_2026H1_2026081801015_c.pdf", "資本開支", "止三個月"),
}
TITLE_PAGE = ("02331_2026H1_2026082001446_c.pdf", None, None)  # 无 CF 表的公告封面页


def _crop(src: Path, pno: int, dest: Path) -> Path:
    doc = pymupdf.open(src)
    try:
        out = pymupdf.open()
        out.insert_pdf(doc, from_page=pno, to_page=pno)
        page = out[0]
        # 剥离图像/字体子集化，把 fixture 压到 ≤300KB
        for img in page.get_images(full=True):
            try:
                page.delete_image(img[0])
            except Exception:
                pass
        try:
            out.subset_fonts()
        except Exception:
            pass
        # 清空 ICC 色彩配置流（CMYK 公告常见 380KB+，裁页 fixture 不需要）
        for xref in range(1, out.xref_length()):
            try:
                t = out.xref_get_key(xref, "Type")
                st = out.xref_get_key(xref, "Subtype")
                n = out.xref_get_key(xref, "N")
                if t == ("null", "null") and st == ("null", "null") and n[0] == "int":
                    out.update_stream(xref, b"")
            except Exception:
                pass
        out.save(dest, garbage=4, deflate=True, deflate_fonts=True, use_objstms=1)
        out.close()
    finally:
        doc.close()
    return dest


@pytest.fixture(scope="session")
def cf_page_pdf():
    """dict: fixture 名 → 裁切后的单页 PDF 路径。"""
    if not SAMPLES.exists():
        pytest.skip("/tmp/hkex_samples 调研样本不在本机", allow_module_level=False)
    FIX_CACHE.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, (fname, marker, extra) in CASES.items():
        src = SAMPLES / fname
        if not src.exists():
            pytest.skip(f"样本缺失: {src}")
        dest = FIX_CACHE / f"{name}.pdf"
        if not dest.exists():
            doc = pymupdf.open(src)
            try:
                pno = None
                for i in range(doc.page_count):
                    t = doc[i].get_text("text")
                    if marker in t and (extra is None or extra in t):
                        pno = i
                        break
                if pno is None:
                    pytest.skip(f"样本 {fname} 中找不到含 {marker!r} 的页")
                _crop(src, pno, dest)
            finally:
                doc.close()
        assert dest.stat().st_size <= 300 * 1024, f"{name} 裁页超 300KB"
        paths[name] = dest
    # 无 CF 页样本（李宁封面页）
    src = SAMPLES / TITLE_PAGE[0]
    dest = FIX_CACHE / "li_title.pdf"
    if not dest.exists():
        _crop(src, 0, dest)
    paths["li_title"] = dest
    return paths
