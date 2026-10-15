import { Suspense } from "react";
import SingleStockPanel from "../../components/SingleStockPanel";

export const metadata = { title: "Single stock · StockScan" };

export default function SingleStockPage() {
  return (
    <>
      <h1 className="page-title">Single stock</h1>
      <p className="subtitle left">
        Candlestick chart with moving averages, Bollinger Bands, support and
        resistance, volume, RSI, MACD and relative strength, computed from the
        prices saved in Azure SQL.
      </p>
      <Suspense>
        <SingleStockPanel />
      </Suspense>
    </>
  );
}
