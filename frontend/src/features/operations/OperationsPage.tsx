import { Page } from "../../components/Page";
import { MoreCommands } from "./MoreCommands";
import { PreflightPanel } from "./PreflightPanel";
import { RunNowPanel } from "./RunNowPanel";
import { SchedulePanel } from "./SchedulePanel";

export default function OperationsPage() {
  return (
    <Page title="Operations" purpose="Set the sync schedule, run a sync by hand, or check the connections first.">
      <SchedulePanel />
      <RunNowPanel />
      <PreflightPanel />
      <MoreCommands />
    </Page>
  );
}
