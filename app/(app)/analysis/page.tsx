import { Suspense } from "react";
import AnalysisPanel from "@/app/components/AnalysisPanel";

export const metadata = { title: "Analysis · StockScan" };

export default function AnalysisPage() {
  return (
    <>
      <h1 className="page-title">Analysis</h1>
      <p className="subtitle left">
        Analyze a stock using the price history saved in Azure SQL. Load a symbol
        on the Load Prices page first.
      </p>
      <Suspense>
        <AnalysisPanel />
      </Suspense>
    </>
  );
}
