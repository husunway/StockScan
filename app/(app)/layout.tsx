import Sidebar from "@/app/components/Sidebar";
import ApiStatus from "@/app/components/ApiStatus";

// Shell for every signed-in page; /login lives outside this group.
export default function AppLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
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
  );
}
