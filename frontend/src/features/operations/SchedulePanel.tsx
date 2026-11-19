import { Power, ShieldAlert } from "lucide-react";
import { useState, type FormEvent } from "react";
import type { ScheduleOut } from "../../api/types";
import { Button } from "../../components/Button";
import { ErrorState } from "../../components/ErrorState";
import { Field } from "../../components/Field";
import { Loading } from "../../components/Loading";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { RadioGroup } from "../../components/RadioGroup";
import { Select } from "../../components/Select";
import { Switch } from "../../components/Switch";
import { Time } from "../../components/Time";
import { formatDateTime, formatUtc } from "../../lib/time";
import { useEnvironment } from "../settings/useEnvironment";
import { useSaveSchedule, useSchedule } from "./api";
import { FREQUENCIES, isDirty, saveErrors, timeZones, toBody, toForm, validate, type FieldErrors, type ScheduleForm } from "./scheduleForm";

function NextRuns({ schedule }: { schedule: ScheduleOut }) {
  if (!schedule.enabled) return <span className="font-medium">paused</span>;
  if (schedule.next_runs.length === 0) return <span>none planned</span>;
  return (
    <>
      {schedule.next_runs.map((r, i) => (
        <span key={r.utc}>
          {i > 0 && ", "}
          <time dateTime={r.utc} title={formatUtc(r.utc)} className="font-mono tabular-nums">
            {formatDateTime(r.utc, schedule.tz)}
          </time>
        </span>
      ))}
      <span className="text-ink-muted"> ({schedule.tz})</span>
    </>
  );
}

function CeilingNotices({ schedule }: { schedule: ScheduleOut }) {
  const production = useEnvironment() === "production";
  return (
    <>
      {!schedule.ceiling_enabled && (
        <Notice tone="neutral" icon={Power} title="Automatic runs are switched off on the server (SCHEDULER_ENABLED).">
          <p>You can still save a schedule; it will apply when the server enables the scheduler.</p>
        </Notice>
      )}
      {production && !schedule.allow_production && (
        <Notice tone="warn" icon={ShieldAlert} title="Production runs will only plan (dry-run) until the server allows them." />
      )}
    </>
  );
}

function ScheduleEditor({ schedule }: { schedule: ScheduleOut }) {
  const save = useSaveSchedule();
  const [draft, setDraft] = useState<ScheduleForm | null>(null);
  const [saved, setSaved] = useState<ScheduleOut | null>(null);
  const [errors, setErrors] = useState<FieldErrors>({});
  const stored = toForm(schedule);
  const form = draft ?? stored;
  const invalid = validate(form);
  const dirty = draft !== null && isDirty(draft, stored);

  const edit = (patch: Partial<ScheduleForm>) => {
    setDraft({ ...form, ...patch });
    setSaved(null);
    setErrors({});
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    save.mutate(toBody(form), {
      onSuccess: (out) => {
        setDraft(null);
        setSaved(out);
        setErrors({});
      },
      onError: (error) => setErrors(saveErrors(error)),
    });
  };

  const shown = saved ?? schedule;
  return (
    <form onSubmit={submit} noValidate className="space-y-5">
      <CeilingNotices schedule={schedule} />
      <Switch label="Automatic sync" checked={form.enabled} onChange={(e) => edit({ enabled: e.target.checked })} hint="When off, nothing runs on its own; you can still run a sync by hand." />
      <RadioGroup legend="How often" options={FREQUENCIES} value={form.every_days} onChange={(every_days) => edit({ every_days })} />
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Time of day (24 h)"
          value={form.time}
          onChange={(e) => edit({ time: e.target.value.trim() })}
          inputMode="numeric"
          autoComplete="off"
          maxLength={5}
          className="font-mono"
          error={invalid.time ?? errors.time}
          hint="HH:MM, for example 02:00."
        />
        <Select label="Time zone" value={form.tz} onChange={(e) => edit({ tz: e.target.value })} error={errors.tz}>
          {timeZones(schedule.tz).map((z) => (
            <option key={z} value={z}>
              {z}
            </option>
          ))}
        </Select>
      </div>
      {form.every_days !== 1 && (
        <Field
          label="Start date (optional)"
          type="date"
          value={form.start_date}
          onChange={(e) => edit({ start_date: e.target.value })}
          className="sm:max-w-xs"
          error={errors.start_date}
          hint="The first run is the next slot on or after this date; later runs follow every N days from it"
        />
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={!dirty || Object.keys(invalid).length > 0 || save.isPending}>
          {save.isPending ? "Saving…" : "Save schedule"}
        </Button>
        {dirty && (
          <Button
            variant="secondary"
            onClick={() => {
              setDraft(null);
              setErrors({});
            }}
          >
            Discard changes
          </Button>
        )}
      </div>
      {errors.form && <ErrorState title="Not saved" message={errors.form} />}
      <div role="status" aria-live="polite" className="space-y-1">
        <p>
          {saved && <span className="font-semibold">Saved. </span>}
          Next runs{dirty ? " (saved schedule)" : ""}: <NextRuns schedule={shown} />
        </p>
      </div>
      <p className="border-t border-rule pt-3 text-table text-ink-muted">
        Source: {schedule.source === "database" ? "saved in the database" : "server default from the environment"}
        {schedule.updated_at && (
          <>
            . Last changed <Time at={schedule.updated_at} />
            {schedule.updated_by && ` by ${schedule.updated_by}`}
          </>
        )}
        .
      </p>
    </form>
  );
}

export function SchedulePanel() {
  const q = useSchedule();
  return (
    <Panel title="Schedule">
      {q.isPending && <Loading label="Loading the schedule" />}
      {q.isError && <ErrorState message="The schedule could not be loaded." onRetry={() => void q.refetch()} />}
      {q.data && <ScheduleEditor schedule={q.data} />}
    </Panel>
  );
}
