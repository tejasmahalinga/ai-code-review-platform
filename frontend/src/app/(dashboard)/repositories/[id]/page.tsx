"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Field,
  Input,
  PageHeader,
  Select,
  Spinner,
  TextLink,
  Textarea,
  errorMessage,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { LLMCredential, Repository, RepositorySettings, ReviewProfile } from "@/lib/types";
import { SEVERITIES } from "@/lib/types";

type FormState = Omit<RepositorySettings, "ignore_patterns" | "effective_model" | "default_ignore_patterns" | "updated_at"> & {
  ignore_text: string;
};

function toForm(s: RepositorySettings): FormState {
  return {
    auto_review: s.auto_review,
    review_drafts: s.review_drafts,
    credential: s.credential,
    model: s.model,
    replace_default_ignores: s.replace_default_ignores,
    custom_instructions: s.custom_instructions,
    min_severity: s.min_severity,
    min_confidence: s.min_confidence,
    max_inline_comments: s.max_inline_comments,
    max_changed_lines: s.max_changed_lines,
    max_files: s.max_files,
    max_input_tokens: s.max_input_tokens,
    chunk_tokens: s.chunk_tokens,
    post_when_no_findings: s.post_when_no_findings,
    profile: s.profile,
    review_on_push: s.review_on_push,
    check_runs: s.check_runs,
    gate_severity: s.gate_severity,
    ignore_text: s.ignore_patterns.join("\n"),
  };
}

export default function RepositorySettingsPage() {
  const { id } = useParams<{ id: string }>();
  const repo = useQuery({ queryKey: ["repository", id], queryFn: () => api<Repository>(`/repositories/${id}`) });
  const settings = useQuery({
    queryKey: ["repository-settings", id],
    queryFn: () => api<RepositorySettings>(`/repositories/${id}/settings`),
    staleTime: Infinity,
  });
  if (repo.error || settings.error) return <Alert>{errorMessage(repo.error ?? settings.error)}</Alert>;
  if (!repo.data || !settings.data) return <Spinner />;
  return <SettingsForm key={id} id={id} repo={repo.data} initial={settings.data} />;
}

function SettingsForm({ id, repo, initial }: { id: string; repo: Repository; initial: RepositorySettings }) {
  const queryClient = useQueryClient();
  const credentials = useQuery({ queryKey: ["llm-credentials"], queryFn: () => api<LLMCredential[]>("/llm-credentials") });
  const profiles = useQuery({ queryKey: ["review-profiles"], queryFn: () => api<ReviewProfile[]>("/review-profiles") });
  const [form, setForm] = useState<FormState>(() => toForm(initial));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: (state: FormState) => {
      const { ignore_text, ...rest } = state;
      return api<RepositorySettings>(`/repositories/${id}/settings`, {
        method: "PATCH",
        body: { ...rest, ignore_patterns: ignore_text.split("\n").map((l) => l.trim()).filter(Boolean) },
      });
    },
    onSuccess: (data) => {
      setForm(toForm(data));
      setSaved(true);
      queryClient.setQueryData(["repository-settings", id], data);
      queryClient.invalidateQueries({ queryKey: ["repositories"] });
      queryClient.invalidateQueries({ queryKey: ["repository", id] });
    },
    onError: (err) => err instanceof ApiError && setErrors(err.fieldErrors()),
  });

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setSaved(false);
    setForm((f) => ({ ...f, [key]: value }));
  };
  const applyProfile = (p: ReviewProfile) => {
    setSaved(false);
    setForm((f) => ({ ...f, profile: p.id, ...p.defaults }));
  };
  const selectedCredential = credentials.data?.find((c) => c.id === form.credential);
  const usable = (credentials.data ?? []).filter((c) => c.status === "valid");

  return (
    <>
      <PageHeader
        title={repo.full_name}
        description={
          <>
            Review settings · <TextLink href="/repositories">All repositories</TextLink> ·{" "}
            <TextLink href={`/pull-requests?repository=${id}`}>Pull requests</TextLink>
          </>
        }
      />
      <form
        className="space-y-6"
        onSubmit={(e) => {
          e.preventDefault();
          setErrors({});
          save.mutate(form);
        }}
      >
        {save.error && <Alert>{errorMessage(save.error)}</Alert>}
        {saved && <Alert kind="success">Settings saved. They apply to the next review.</Alert>}

        <Card title="Model">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="LLM key" error={errors.credential}>
              <Select
                value={form.credential ?? ""}
                onChange={(e) => update("credential", e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">— none —</option>
                {usable.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} ({c.provider})
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label="Model override"
              hint={`Leave empty to use the key's default${selectedCredential ? ` (${selectedCredential.default_model})` : ""}.`}
              error={errors.model}
            >
              <Input value={form.model} onChange={(e) => update("model", e.target.value)} placeholder={selectedCredential?.default_model} />
            </Field>
          </div>
          {usable.length === 0 && (
            <p className="mt-3 text-sm text-amber-700">
              No valid LLM keys. <TextLink href="/settings/keys">Add one first.</TextLink>
            </p>
          )}
        </Card>

        <Card title="Triggers">
          <div className="space-y-3">
            <Checkbox
              label="Review automatically when a pull request is opened, reopened, or marked ready"
              checked={form.auto_review}
              onChange={(e) => update("auto_review", e.target.checked)}
            />
            <Checkbox
              label="Review new commits pushed to open pull requests"
              hint="Only the changes since the last reviewed commit are sent to the LLM. Bursts of pushes are debounced."
              checked={form.review_on_push}
              onChange={(e) => update("review_on_push", e.target.checked)}
            />
            <Checkbox label="Also review draft pull requests" checked={form.review_drafts} onChange={(e) => update("review_drafts", e.target.checked)} />
            <Checkbox
              label="Post a summary even when nothing is found"
              checked={form.post_when_no_findings}
              onChange={(e) => update("post_when_no_findings", e.target.checked)}
            />
          </div>
        </Card>

        <Card title="Review profile">
          <div className="grid gap-3 md:grid-cols-4">
            {profiles.data?.map((p) => (
              <label
                key={p.id}
                className={`cursor-pointer rounded-md border p-3 text-sm ${
                  form.profile === p.id ? "border-slate-900 ring-1 ring-slate-900" : "border-slate-200 hover:border-slate-400"
                }`}
              >
                <input
                  type="radio"
                  name="profile"
                  className="sr-only"
                  checked={form.profile === p.id}
                  onChange={() => applyProfile(p)}
                />
                <span className="block font-medium">{p.label}</span>
                <span className="mt-1 block text-xs text-slate-600">{p.description}</span>
                <span className="mt-2 block text-xs text-slate-500">
                  {p.defaults.min_severity}+ · confidence {p.defaults.min_confidence} · {p.defaults.max_inline_comments} comments
                </span>
              </label>
            ))}
          </div>
          <p className="mt-3 text-xs text-slate-500">
            Choosing a profile sets the review focus and fills in the thresholds below. You can still adjust them.
          </p>
        </Card>

        <Card title="GitHub check">
          <div className="grid gap-4 md:grid-cols-2">
            <Checkbox
              label="Report a “Reviewbot” check run on each reviewed commit"
              hint="Requires the Checks permission on the GitHub App."
              checked={form.check_runs}
              onChange={(e) => update("check_runs", e.target.checked)}
            />
            <Field
              label="Fail the check when a finding is at least"
              hint="Use with branch protection to block merging. A failed review never fails the check."
              error={errors.gate_severity}
            >
              <Select
                value={form.gate_severity}
                disabled={!form.check_runs}
                onChange={(e) => update("gate_severity", e.target.value as FormState["gate_severity"])}
              >
                <option value="">Never fail (neutral)</option>
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        </Card>

        <Card title="What gets posted">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Minimum severity" error={errors.min_severity}>
              <Select value={form.min_severity} onChange={(e) => update("min_severity", e.target.value as FormState["min_severity"])}>
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Minimum confidence" hint="0 – 1" error={errors.min_confidence}>
              <Input type="number" step="0.05" min={0} max={1} value={form.min_confidence} onChange={(e) => update("min_confidence", Number(e.target.value))} />
            </Field>
            <Field label="Max inline comments per review" error={errors.max_inline_comments}>
              <Input type="number" min={0} max={100} value={form.max_inline_comments} onChange={(e) => update("max_inline_comments", Number(e.target.value))} />
            </Field>
          </div>
        </Card>

        <Card title="Instructions">
          <Field
            label="Custom review instructions"
            hint={`${form.custom_instructions.length}/4000 · Appended to the prompt, e.g. “We use Django; flag raw SQL. Ignore missing docstrings.”`}
            error={errors.custom_instructions}
          >
            <Textarea rows={5} maxLength={4000} value={form.custom_instructions} onChange={(e) => update("custom_instructions", e.target.value)} />
          </Field>
        </Card>

        <Card title="Ignored files">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Ignore patterns" hint="One gitignore-style pattern per line. Use !pattern to re-include." error={errors.ignore_patterns}>
              <Textarea rows={8} value={form.ignore_text} onChange={(e) => update("ignore_text", e.target.value)} placeholder={"docs/\n*.md\n!docs/security.md"} />
            </Field>
            <div className="space-y-2">
              <Checkbox
                label="Replace the built-in defaults"
                hint="By default your patterns are added to the built-in list."
                checked={form.replace_default_ignores}
                onChange={(e) => update("replace_default_ignores", e.target.checked)}
              />
              <details className="text-sm">
                <summary className="cursor-pointer text-slate-600">Built-in defaults</summary>
                <pre className="mt-2 max-h-48 overflow-auto rounded bg-slate-50 p-2 text-xs">{initial.default_ignore_patterns.join("\n")}</pre>
              </details>
            </div>
          </div>
        </Card>

        <Card title="Limits (cost guard)">
          <div className="grid gap-4 md:grid-cols-4">
            <Field label="Max changed lines" error={errors.max_changed_lines}>
              <Input type="number" min={1} value={form.max_changed_lines} onChange={(e) => update("max_changed_lines", Number(e.target.value))} />
            </Field>
            <Field label="Max files" error={errors.max_files}>
              <Input type="number" min={1} max={3000} value={form.max_files} onChange={(e) => update("max_files", Number(e.target.value))} />
            </Field>
            <Field label="Max input tokens per review" error={errors.max_input_tokens}>
              <Input type="number" min={1000} value={form.max_input_tokens} onChange={(e) => update("max_input_tokens", Number(e.target.value))} />
            </Field>
            <Field label="Tokens per LLM request" error={errors.chunk_tokens}>
              <Input type="number" min={1000} value={form.chunk_tokens} onChange={(e) => update("chunk_tokens", Number(e.target.value))} />
            </Field>
          </div>
          <p className="mt-3 text-xs text-slate-500">
            Pull requests above these limits are skipped with an explanatory comment and make no LLM calls.
          </p>
        </Card>

        <div className="flex gap-2">
          <Button type="submit" loading={save.isPending}>
            Save settings
          </Button>
        </div>
      </form>
    </>
  );
}
