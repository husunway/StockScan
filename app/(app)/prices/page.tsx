import { Suspense } from "react";
import PriceLoader from "@/app/components/PriceLoader";

export const metadata = { title: "Load Prices · StockScan" };

export default function PricesPage() {
  return (
    <>
      <h1 className="page-title">Load Prices</h1>
      <p className="subtitle left">
        Downloads the last two years of daily prices from Yahoo Finance and saves
        them to the Azure SQL table <code>dbo.stock_prices</code>.
      </p>
      <Suspense>
        <PriceLoader />
      </Suspense>
    </>
  );
}
