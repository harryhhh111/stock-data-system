export type Market = "CN_A" | "CN_HK" | "US";
export type Severity = "error" | "warning" | "info";

export interface ApiResponse<T> {
  ok: boolean;
  data?: T;
  error?: string;
  detail?: string;
}

export interface Paginated<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}