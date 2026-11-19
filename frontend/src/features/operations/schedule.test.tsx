import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { ScheduleIn } from "../../api/types";
import { scheduleOut } from "../../test/fixtures";
import { openOperations } from "../../test/operations";
import { http, HttpResponse, server } from "../../test/server";
import { isDirty, saveErrors, TIME_PATTERN, toBody, toForm, validate } from "./scheduleForm";
import { ApiError } from "../../api/client";

const save = () => screen.getByRole("button", { name: "Save schedule" });
const timeField = () => screen.getByLabelText("Time of day (24 h)");

async function setTime(user: ReturnType<typeof userEvent.setup>, value: string) {
  await user.clear(timeField());
  await user.type(timeField(), value);
}

/** A PUT handler that records its bodies and answers with `reply`. */
function putHandler(reply: () => Response, bodies: ScheduleIn[] = []) {
  return http.put("*/api/v1/schedule", async ({ request }) => {
    bodies.push((await request.json()) as ScheduleIn);
    return reply();
  });
}

describe("schedule form", () => {
  it("shows the stored schedule, its source, who changed it and the next runs; save starts disabled", async () => {
    openOperations();
    expect(await screen.findByLabelText("Time of day (24 h)")).toHaveValue("02:00");
    expect(screen.getByRole("switch", { name: /Automatic sync/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Every day" })).toBeChecked();
    expect(screen.getByLabelText("Time zone")).toHaveValue("UTC");
    expect(screen.getByText(/Source: saved in the database/)).toHaveTextContent(/Last changed .* by ada/);
    const runs = screen.getByText(/^Next runs:/);
    expect(runs.querySelectorAll("time")).toHaveLength(3);
    expect(runs.querySelector("time")).toHaveAttribute("title", expect.stringMatching(/UTC$/));
    expect(save()).toBeDisabled();
  });

  it("says when the schedule is the server default", async () => {
    openOperations({ schedule: scheduleOut({ source: "env_default", updated_at: null, updated_by: null }) });
    expect(await screen.findByText(/Source: server default from the environment/)).toBeInTheDocument();
  });

  it("is dirty only after a change, and Discard puts it back", async () => {
    const user = userEvent.setup();
    openOperations();
    await screen.findByLabelText("Time of day (24 h)");
    await user.click(screen.getByRole("switch", { name: /Automatic sync/ }));
    expect(save()).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(save()).toBeDisabled();
    expect(screen.getByRole("switch", { name: /Automatic sync/ })).toBeChecked();
  });

  it("validates the time like the backend: 24 h HH:MM only", async () => {
    const user = userEvent.setup();
    openOperations();
    await screen.findByLabelText("Time of day (24 h)");
    for (const bad of ["24:00", "2:00", "12:60", "ab:cd", ""]) {
      if (bad === "") await user.clear(timeField());
      else await setTime(user, bad);
      expect(screen.getByText("Use 24-hour HH:MM, from 00:00 to 23:59.")).toBeInTheDocument();
      expect(timeField()).toHaveAttribute("aria-invalid", "true");
      expect(save()).toBeDisabled();
    }
    await setTime(user, "23:59");
    expect(screen.queryByText("Use 24-hour HH:MM, from 00:00 to 23:59.")).not.toBeInTheDocument();
    expect(save()).toBeEnabled();
    expect(["00:00", "23:59", "09:05"].every((t) => TIME_PATTERN.test(t))).toBe(true);
  });

  it("offers exactly three frequencies and shows the start date only for 3 days and a week", async () => {
    const user = userEvent.setup();
    openOperations();
    const group = await screen.findByRole("group", { name: "How often" });
    expect(within(group).getAllByRole("radio").map((r) => r.getAttribute("value"))).toEqual(["1", "3", "7"]);
    expect(screen.queryByLabelText("Start date (optional)")).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Every 3 days" }));
    expect(screen.getByLabelText("Start date (optional)")).toBeInTheDocument();
    expect(screen.getByText("The first run is the next slot on or after this date; later runs follow every N days from it")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Every week" }));
    expect(screen.getByLabelText("Start date (optional)")).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Every day" }));
    expect(screen.queryByLabelText("Start date (optional)")).not.toBeInTheDocument();
  });

  it("sends each frequency; the start date goes only with 3 days / a week (null means today)", async () => {
    const user = userEvent.setup();
    const bodies: ScheduleIn[] = [];
    server.use(putHandler(() => HttpResponse.json(scheduleOut({ every_days: 7 })), bodies));
    openOperations();
    await screen.findByLabelText("Time of day (24 h)");
    await user.click(screen.getByRole("radio", { name: "Every week" }));
    fireEvent.change(screen.getByLabelText("Start date (optional)"), { target: { value: "2026-12-01" } });
    await user.click(save());
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ enabled: true, every_days: 7, time: "02:00", tz: "UTC", start_date: "2026-12-01" });
    expect(toBody({ ...toForm(scheduleOut()), every_days: 1, start_date: "2026-12-01" }).start_date).toBeNull();
    expect(toBody({ ...toForm(scheduleOut()), every_days: 3, start_date: "" }).start_date).toBeNull();
  });

  it("the start date alone never makes a daily schedule dirty", () => {
    const stored = toForm(scheduleOut());
    expect(isDirty({ ...stored, start_date: "2026-12-31" }, stored)).toBe(false);
    expect(isDirty({ ...stored, every_days: 3 }, stored)).toBe(true);
    expect(validate({ ...stored, time: "7:00" })).toHaveProperty("time");
  });

  it("builds the time zone list from the browser, and always includes the stored value", async () => {
    openOperations({ schedule: scheduleOut({ tz: "Mars/Olympus" }) });
    const select = await screen.findByLabelText("Time zone");
    expect(select).toHaveValue("Mars/Olympus");
    const options = within(select).getAllByRole("option").map((o) => o.getAttribute("value"));
    expect(options).toContain("Mars/Olympus");
    expect(options).toContain("America/Toronto");
    expect(options).toContain("UTC");
    expect(new Set(options).size).toBe(options.length);
  });

  it("after a save: 'Saved. Next runs: ...' in the chosen zone with UTC on hover, then save is disabled again", async () => {
    const user = userEvent.setup();
    const saved = scheduleOut({
      time: "03:30",
      tz: "America/Toronto",
      next_runs: [20, 21, 22].map((d) => ({ utc: `2026-11-${d}T08:30:00Z`, local: `2026-11-${d}T03:30:00-05:00` })),
    });
    server.use(putHandler(() => HttpResponse.json(saved)));
    openOperations();
    await screen.findByLabelText("Time of day (24 h)");
    await setTime(user, "03:30");
    await user.selectOptions(screen.getByLabelText("Time zone"), "America/Toronto");
    await user.click(save());
    const line = (await screen.findByText("Saved.")).closest("p") as HTMLElement;
    const times = line.querySelectorAll("time");
    expect(times).toHaveLength(3);
    expect(times[0]).toHaveTextContent("03:30");
    expect(times[0]).toHaveAttribute("title", expect.stringContaining("08:30:00 UTC"));
    expect(line).toHaveTextContent("(America/Toronto)");
    expect(save()).toBeDisabled();
  });

  it("shows 'paused' instead of runs while the schedule is off", async () => {
    openOperations({ schedule: scheduleOut({ enabled: false, next_runs: [] }) });
    expect(await screen.findByText(/^Next runs:/)).toHaveTextContent("Next runs: paused");
  });

  it("maps start_date_out_of_range to a plain message on the field", async () => {
    const user = userEvent.setup();
    server.use(putHandler(() => HttpResponse.json({ detail: "start_date_out_of_range" }, { status: 422 })));
    openOperations();
    await screen.findByLabelText("Time of day (24 h)");
    await user.click(screen.getByRole("radio", { name: "Every 3 days" }));
    fireEvent.change(screen.getByLabelText("Start date (optional)"), { target: { value: "2031-01-01" } });
    await user.click(save());
    expect(await screen.findByText("Pick a start date within one year of today.")).toBeInTheDocument();
    expect(screen.getByLabelText("Start date (optional)")).toHaveAttribute("aria-invalid", "true");
  });

  it("maps a 422 field list (time, tz) to the matching fields, with no toast", async () => {
    const user = userEvent.setup();
    const detail = [
      { loc: ["body", "time"], msg: "String should match pattern", type: "string_pattern_mismatch" },
      { loc: ["body", "tz"], msg: "Value error, unknown time zone", type: "value_error" },
    ];
    server.use(putHandler(() => HttpResponse.json({ detail }, { status: 422 })));
    openOperations();
    await screen.findByLabelText("Time of day (24 h)");
    await user.click(screen.getByRole("switch", { name: /Automatic sync/ }));
    await user.click(save());
    expect(await screen.findByText("That time zone is not recognised. Pick one from the list.")).toBeInTheDocument();
    expect(screen.getByText("Use 24-hour HH:MM, from 00:00 to 23:59.")).toBeInTheDocument();
    expect(saveErrors(new ApiError(422, [{ loc: ["body", "every_days"] }]))).toEqual({ form: "The server did not accept these values." });
  });

  it("a network error keeps the draft and says so", async () => {
    const user = userEvent.setup();
    server.use(http.put("*/api/v1/schedule", () => HttpResponse.error()));
    openOperations();
    await screen.findByLabelText("Time of day (24 h)");
    await user.click(screen.getByRole("switch", { name: /Automatic sync/ }));
    await user.click(save());
    expect(await screen.findByText(/could not be saved/)).toBeInTheDocument();
    expect(save()).toBeEnabled();
  });

  it("explains the server ceiling calmly and still lets you save", async () => {
    openOperations({ schedule: scheduleOut({ ceiling_enabled: false }) });
    expect(await screen.findByText("Automatic runs are switched off on the server (SCHEDULER_ENABLED).")).toBeInTheDocument();
    expect(screen.getByText("You can still save a schedule; it will apply when the server enables the scheduler.")).toBeInTheDocument();
  });

  it("production without the allow flag: plan-only notice; test environment: none", async () => {
    const first = openOperations({ schedule: scheduleOut({ allow_production: false }), settings: { environment: "production" } });
    expect(await screen.findByText("Production runs will only plan (dry-run) until the server allows them.")).toBeInTheDocument();
    first.unmount();
    openOperations({ schedule: scheduleOut({ allow_production: false }), settings: { environment: "test" } });
    await screen.findByLabelText("Time of day (24 h)");
    expect(screen.queryByText(/will only plan/)).not.toBeInTheDocument();
  });

  it("first use: server default -> weekly -> save sends start_date null and succeeds", async () => {
    const user = userEvent.setup();
    const bodies: ScheduleIn[] = [];
    server.use(putHandler(() => HttpResponse.json(scheduleOut({ every_days: 7, start_date: "2026-11-19" })), bodies));
    openOperations({ schedule: scheduleOut({ source: "env_default", start_date: null, updated_at: null, updated_by: null }) });
    await screen.findByLabelText("Time of day (24 h)");
    await user.click(screen.getByRole("radio", { name: "Every week" }));
    expect(screen.getByLabelText("Start date (optional)")).toHaveValue("");
    await user.click(save());
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ every_days: 7, start_date: null });
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("a stale anchor on a server default is never copied into the form", () => {
    expect(toForm(scheduleOut({ source: "env_default", every_days: 3, start_date: "2020-01-01" })).start_date).toBe("");
    expect(toForm(scheduleOut({ every_days: 1, start_date: "2020-01-01" })).start_date).toBe("");
    expect(toForm(scheduleOut({ every_days: 3, start_date: null })).start_date).toBe("");
  });

  it("editing the time of an existing 3-day schedule keeps its anchor", async () => {
    const user = userEvent.setup();
    const bodies: ScheduleIn[] = [];
    server.use(putHandler(() => HttpResponse.json(scheduleOut({ every_days: 3, start_date: "2026-11-01", time: "04:00" })), bodies));
    openOperations({ schedule: scheduleOut({ every_days: 3, start_date: "2026-11-01" }) });
    await screen.findByLabelText("Time of day (24 h)");
    expect(screen.getByLabelText("Start date (optional)")).toHaveValue("2026-11-01");
    await setTime(user, "04:00");
    await user.click(save());
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ every_days: 3, time: "04:00", start_date: "2026-11-01" });
  });

  it("daily hides the start date and sends null", async () => {
    const user = userEvent.setup();
    const bodies: ScheduleIn[] = [];
    server.use(putHandler(() => HttpResponse.json(scheduleOut()), bodies));
    openOperations({ schedule: scheduleOut({ every_days: 3, start_date: "2026-11-01" }) });
    await screen.findByLabelText("Time of day (24 h)");
    await user.click(screen.getByRole("radio", { name: "Every day" }));
    expect(screen.queryByLabelText("Start date (optional)")).not.toBeInTheDocument();
    await user.click(save());
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ every_days: 1, start_date: null });
  });

  it("labels the next runs as those of the saved schedule while the draft differs", async () => {
    const user = userEvent.setup();
    openOperations();
    expect(await screen.findByText(/^Next runs:/)).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: /Automatic sync/ }));
    expect(screen.getByText(/^Next runs \(saved schedule\):/)).toBeInTheDocument();
  });
});
