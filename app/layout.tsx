import type { Metadata } from "next";
import Sidebar from "./components/Sidebar";
import ApiStatus from "./components/ApiStatus";
import "./globals.css";

export const metadata: Metadata = {
  title: "StockScan",
  description: "Stock analysis dashboard",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <Sidebar />
          <div className="content">
            <main className="main">{children}</main>
            <footer className="footer">
              <ApiStatus />
              <span>For informational purposes only. Not investment advice.</span>
            </footer>
          </div>
        </div>
      </body>
    </html>
  );
}
