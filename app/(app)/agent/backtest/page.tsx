import { Suspense } from "react";
import BacktestPanel from "@/app/components/BacktestPanel";

export const metadata = { title: "Weekly backtest · StockScan" };

export default function BacktestPage() {
  return (
    <>
      <h1 className="page-title">Weekly backtest</h1>
      <p className="subtitle left">
        Runs a skill as of every Monday of a year, as if the analyst were working on that date, and
        records each decision.
      </p>
      <Suspense>
        <BacktestPanel />
      </Suspense>
    </>
  );
}
