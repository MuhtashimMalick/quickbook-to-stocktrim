/** Actions the web app does not offer (the API allows them only from the server itself). Ids come from the API, never from user input. */
const cli = (action: "cancel" | "resume", runId: string) => `echo '{}' | docker compose exec -T app python -m app.cli POST /push/runs/${runId}/${action} --json`;

export const cancelCommand = (runId: string) => cli("cancel", runId);
export const resumeCommand = (runId: string) => cli("resume", runId);
