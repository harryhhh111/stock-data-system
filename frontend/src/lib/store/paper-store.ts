import { create } from "zustand";
import { persist } from "zustand/middleware";

interface PaperFilterState {
  statusFilter: string;
  setStatusFilter: (v: string) => void;
}

export const usePaperFilterStore = create<PaperFilterState>()(
  persist(
    (set) => ({
      statusFilter: "active",
      setStatusFilter: (v) => set({ statusFilter: v }),
    }),
    { name: "paper-filters" },
  ),
);
