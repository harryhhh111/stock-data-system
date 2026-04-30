import { Routes, Route, Navigate } from "react-router-dom";
import { AppLayout } from "@/components/layout/app-layout";
import { DashboardPage } from "@/pages/dashboard-page";
import { SyncPage } from "@/pages/sync-page";
import { QualityPage } from "@/pages/quality-page";
import { ScreenerPage } from "@/pages/screener-page";
import { AnalyzerPage } from "@/pages/analyzer-page";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="sync" element={<SyncPage />} />
        <Route path="quality" element={<QualityPage />} />
        <Route path="screener" element={<ScreenerPage />} />
        <Route path="analyzer" element={<AnalyzerPage />} />
      </Route>
    </Routes>
  );
}