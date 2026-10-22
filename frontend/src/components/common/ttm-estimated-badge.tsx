/** TTM NULL 回退口径徽标（docs/quant/TTM_NULL_FALLBACK_PLAN.md）。
 * fcf_ttm_estimated=true 表示 FCF TTM 为年度代理值：
 * 中报未披露该分量，用最近年报估算，最长滞后约 11 个月。
 */
export function TtmEstimatedBadge() {
  return (
    <span
      title="TTM 为年度代理值：中报未披露，用最近年报估算"
      className="ml-0.5 cursor-help text-xs font-normal text-muted-foreground"
    >
      ≈
    </span>
  );
}
