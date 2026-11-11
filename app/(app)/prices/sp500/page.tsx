import UniverseLoader from "@/app/components/UniverseLoader";

export const metadata = { title: "Index universe · StockScan" };

export default function Sp500Page() {
  return (
    <>
      <h1 className="page-title">Index universe</h1>
      <p className="subtitle left">
        Load price history and prepare the technical data (TECH_ tables) for every S&amp;P 500 or NASDAQ 100 constituent,
        with live progress. Only missing days and stale technical data are processed, so re-running is cheap.
      </p>
      <UniverseLoader />
    </>
  );
}
