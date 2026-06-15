import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Market } from "@/lib/types/common";

interface BacktestState {
  presetName: string;
  market: Market;
  start: string;
  end: string;
  months: number;
  topN: number | null;
  capital: number;
  benchmark: string;
  timing: boolean;
  setPresetName: (v: string) => void;
  setMarket: (v: Market) => void;
  setStart: (v: string) => void;
  setEnd: (v: string) => void;
  setMonths: (v: number) => void;
  setTopN: (v: number | null) => void;
  setCapital: (v: number) => void;
  setBenchmark: (v: string) => void;
  setTiming: (v: boolean) => void;
}

export const useBacktestStore = create<BacktestState>()(
  persist(
    (set) => ({
      presetName: "fcf_roe_value",
      market: "CN_A" as Market,
      start: "2022-01",
      end: "",
      months: 6,
      topN: null,
      capital: 1_000_000,
      benchmark: "",
      timing: false,
      setPresetName: (v) => set({ presetName: v }),
      setMarket: (v) => set({ market: v }),
      setStart: (v) => set({ start: v }),
      setEnd: (v) => set({ end: v }),
      setMonths: (v) => set({ months: v }),
      setTopN: (v) => set({ topN: v }),
      setCapital: (v) => set({ capital: v }),
      setBenchmark: (v) => set({ benchmark: v }),
      setTiming: (v) => set({ timing: v }),
    }),
    { name: "backtest-filters" },
  ),
);
