import { Routes, Route, Navigate } from "react-router-dom";
import { AppLayout } from "@/components/layout/app-layout";
import { DashboardPage } from "@/pages/dashboard-page";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="sync" element={<div>Sync</div>} />
        <Route path="quality" element={<div>Quality</div>} />
        <Route path="screener" element={<div>Screener</div>} />
        <Route path="analyzer" element={<div>Analyzer</div>} />
      </Route>
    </Routes>
  );
}