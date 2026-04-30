import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { StockSearchResult } from "@/lib/types/analyzer";

const MAX_HISTORY = 20;

interface AnalyzerState {
  history: StockSearchResult[];
  addHistory: (stock: StockSearchResult) => void;
  clearHistory: () => void;
}

export const useAnalyzerStore = create<AnalyzerState>()(
  persist(
    (set) => ({
      history: [],
      addHistory: (stock) =>
        set((state) => {
          const filtered = state.history.filter(
            (s) => !(s.stock_code === stock.stock_code && s.market === stock.market),
          );
          return { history: [stock, ...filtered].slice(0, MAX_HISTORY) };
        }),
      clearHistory: () => set({ history: [] }),
    }),
    { name: "analyzer-history" },
  ),
);
