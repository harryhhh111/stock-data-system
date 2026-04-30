import { useState, useEffect, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { Command } from "cmdk";
import { Badge } from "@/components/ui/badge";
import { Search, Clock, X } from "lucide-react";
import { useAnalyzerStore } from "@/lib/store/analyzer-store";
import { analyzerApi } from "@/lib/api/client";
import type { Market } from "@/lib/types/common";
import type { StockSearchResult } from "@/lib/types/analyzer";

interface Props {
  market: Market | "all";
  onSelect: (stock: StockSearchResult) => void;
}

export function StockSearch({ market, onSelect }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const { history, clearHistory } = useAnalyzerStore();

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 200);
    return () => clearTimeout(timer);
  }, [query]);

  const { data: results } = useQuery({
    queryKey: ["analyzer", "search", debouncedQuery, market],
    queryFn: () => analyzerApi.search(debouncedQuery, market === "all" ? undefined : market),
    enabled: debouncedQuery.length >= 2,
    staleTime: 60_000,
  });

  const handleSelect = useCallback((stock: StockSearchResult) => {
    setOpen(false);
    setQuery("");
    onSelect(stock);
  }, [onSelect]);

  // Keyboard shortcut: Cmd/Ctrl + K
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  const showHistory = !debouncedQuery && history.length > 0;
  const showResults = debouncedQuery.length >= 2 && results && results.length > 0;

  return (
    <div className="relative">
      {/* Trigger */}
      <button
        className="flex items-center gap-2 w-full max-w-md px-3 py-2 rounded-md border bg-card text-sm text-muted-foreground hover:border-ring transition-colors"
        onClick={() => setOpen(true)}
      >
        <Search className="h-4 w-4" />
        <span>搜索股票代码或名称...</span>
        <kbd className="ml-auto pointer-events-none inline-flex h-5 select-none items-center gap-1 rounded border bg-muted px-1.5 font-mono text-[10px] font-medium text-muted-foreground">
          <span className="text-xs">⌘</span>K
        </kbd>
      </button>

      {/* Command palette overlay */}
      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center pt-[20vh]">
          <div className="fixed inset-0 bg-black/50" onClick={() => setOpen(false)} />
          <Command
            className="relative z-50 w-full max-w-lg rounded-lg border bg-popover shadow-lg overflow-hidden"
            shouldFilter={false}
          >
            <div className="flex items-center border-b px-3">
              <Search className="h-4 w-4 text-muted-foreground shrink-0" />
              <Command.Input
                className="flex h-11 w-full rounded-md bg-transparent py-3 px-2 text-sm outline-none placeholder:text-muted-foreground"
                placeholder="输入股票代码或名称..."
                value={query}
                onValueChange={setQuery}
                autoFocus
              />
              {query && (
                <button className="text-muted-foreground hover:text-foreground" onClick={() => setQuery("")}>
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>
            <Command.List className="max-h-80 overflow-y-auto p-1">
              {/* History */}
              {showHistory && (
                <Command.Group heading="最近搜索">
                  <div className="flex items-center justify-between px-2 py-1">
                    <span className="text-xs text-muted-foreground" />
                    <button
                      className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
                      onClick={(e) => { e.stopPropagation(); clearHistory(); }}
                    >
                      <X className="h-3 w-3" /> 清空
                    </button>
                  </div>
                  {history.map((stock) => (
                    <Command.Item
                      key={`h-${stock.stock_code}-${stock.market}`}
                      value={`history-${stock.stock_code}`}
                      onSelect={() => handleSelect(stock)}
                      className="flex items-center justify-between px-3 py-2 rounded-md cursor-pointer text-sm aria-selected:bg-accent"
                    >
                      <span className="flex items-center gap-2">
                        <Clock className="h-3.5 w-3.5 text-muted-foreground" />
                        <span className="font-medium">{stock.stock_name}</span>
                        <span className="text-muted-foreground text-xs">{stock.stock_code}</span>
                      </span>
                      <Badge variant="outline" className="text-xs">{stock.market}</Badge>
                    </Command.Item>
                  ))}
                </Command.Group>
              )}

              {/* Search results */}
              {showResults && (
                <Command.Group heading="搜索结果">
                  {results!.slice(0, 15).map((stock) => (
                    <Command.Item
                      key={`${stock.stock_code}-${stock.market}`}
                      value={`${stock.stock_code}-${stock.stock_name}`}
                      onSelect={() => handleSelect(stock)}
                      className="flex items-center justify-between px-3 py-2 rounded-md cursor-pointer text-sm aria-selected:bg-accent"
                    >
                      <span>
                        <span className="font-medium">{stock.stock_name}</span>
                        <span className="text-muted-foreground ml-2 text-xs">{stock.stock_code}</span>
                      </span>
                      <span className="flex items-center gap-2">
                        {stock.industry && <span className="text-xs text-muted-foreground">{stock.industry}</span>}
                        <Badge variant="outline" className="text-xs">{stock.market}</Badge>
                      </span>
                    </Command.Item>
                  ))}
                </Command.Group>
              )}

              {/* Empty state */}
              {debouncedQuery.length >= 2 && results && results.length === 0 && (
                <Command.Empty className="py-6 text-center text-sm text-muted-foreground">
                  未找到匹配的股票
                </Command.Empty>
              )}

              {/* Hint when typing */}
              {query.length > 0 && query.length < 2 && (
                <div className="py-4 text-center text-sm text-muted-foreground">
                  输入至少 2 个字符开始搜索
                </div>
              )}

              {/* Initial state */}
              {!query && history.length === 0 && (
                <div className="py-6 text-center text-sm text-muted-foreground">
                  输入股票代码或名称开始搜索
                </div>
              )}
            </Command.List>
          </Command>
        </div>
      )}
    </div>
  );
}
