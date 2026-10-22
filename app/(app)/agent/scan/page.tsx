import ScanPanel from "@/app/components/ScanPanel";

export const metadata = { title: "S&P 500 scan · StockScan" };

export default function ScanPage() {
  return (
    <>
      <h1 className="page-title">S&amp;P 500 scan</h1>
      <p className="subtitle left">
        For each date and each constituent: a free technical pre-screen, then John Murphy; a BUY with the
        close inside the entry zone goes to Warren Buffett; a Buffett BUY goes to Jamie Dimon. A stock that
        passes all three is a hit. Rejections stop early to save model cost. Hits appear on the calendar as
        the scan runs.
      </p>
      <ScanPanel />
    </>
  );
}
