import { create } from "zustand";
import type { ScreenerResult } from "@/lib/types/screener";

interface ScreenerResultState {
  lastResult: ScreenerResult | null;
  setLastResult: (result: ScreenerResult | null) => void;
}

/** 筛选结果 in-memory 存储：切换 tab 不丢，刷新页面清空。 */
export const useScreenerResultStore = create<ScreenerResultState>()((set) => ({
  lastResult: null,
  setLastResult: (result) => set({ lastResult: result }),
}));
