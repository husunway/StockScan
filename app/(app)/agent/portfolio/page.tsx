import PortfolioPanel from "@/app/components/PortfolioPanel";

export const metadata = { title: "Portfolio backtest · StockScan" };

export default function PortfolioPage() {
  return (
    <>
      <h1 className="page-title">Portfolio backtest</h1>
      <p className="subtitle left">
        Simulates a daily trading strategy over the first N S&amp;P 500 stocks. A buy is recorded only when
        Warren Buffett, Jamie Dimon and John Murphy all recommend buying at the current price; a sell when
        the lowest agent target or the take-profit level is reached. Cash, positions, P&amp;L and the trade
        log update live as the simulation runs, and every agent run is a stored decision with its reports.
      </p>
      <PortfolioPanel />
    </>
  );
}
