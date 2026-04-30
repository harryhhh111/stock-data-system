import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Market } from "@/lib/types/common";

interface ScreenerState {
  market: Market;
  preset: string;
  topN: number;
  setMarket: (market: Market) => void;
  setPreset: (preset: string) => void;
  setTopN: (topN: number) => void;
}

export const useScreenerStore = create<ScreenerState>()(
  persist(
    (set) => ({
      market: "CN_A",
      preset: "classic_value",
      topN: 50,
      setMarket: (market) => set({ market }),
      setPreset: (preset) => set({ preset }),
      setTopN: (topN) => set({ topN }),
    }),
    { name: "screener-filters" },
  ),
);
