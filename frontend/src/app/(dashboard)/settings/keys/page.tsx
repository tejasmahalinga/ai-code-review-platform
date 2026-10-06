"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Empty,
  Field,
  Input,
  PageHeader,
  Select,
  Spinner,
  errorMessage,
  formatDate,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { BudgetBar } from "@/components/budget";
import type { LLMCredential, LLMProviderInfo, ModelPrice } from "@/lib/types";

const STATUS_TONE = { valid: "green", invalid: "red", revoked: "slate" } as const;

export default function KeysPage() {
  const queryClient = useQueryClient();
  const credentials = useQuery({ queryKey: ["llm-credentials"], queryFn: () => api<LLMCredential[]>("/llm-credentials") });
  const providers = useQuery({ queryKey: ["llm-providers"], queryFn: () => api<LLMProviderInfo[]>("/llm-providers") });
  const [showForm, setShowForm] = useState(false);
  const [rotating, setRotating] = useState<number | null>(null);
  const [budgeting, setBudgeting] = useState<number | null>(null);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["llm-credentials"] });
  const validate = useMutation({
    mutationFn: (id: number) => api<LLMCredential>(`/llm-credentials/${id}/validate`, { method: "POST" }),
    onSuccess: invalidate,
  });
  const revoke = useMutation({
    mutationFn: (id: number) => api(`/llm-credentials/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });

  const providerLabel = (id: string) => providers.data?.find((p) => p.id === id)?.label ?? id;

  return (
    <>
      <PageHeader
        title="LLM keys"
        description="Bring your own key. Keys are encrypted at rest and never shown again after saving."
        actions={!showForm && <Button onClick={() => setShowForm(true)}>Add key</Button>}
      />
      {showForm && providers.data && (
        <div className="mb-6">
          <AddKeyForm
            providers={providers.data}
            onDone={() => {
              setShowForm(false);
              invalidate();
            }}
            onCancel={() => setShowForm(false)}
          />
        </div>
      )}
      {(validate.error || revoke.error) && (
        <div className="mb-4">
          <Alert>{errorMessage(validate.error ?? revoke.error)}</Alert>
        </div>
      )}
      {credentials.isLoading ? (
        <Spinner />
      ) : credentials.error ? (
        <Alert>{errorMessage(credentials.error)}</Alert>
      ) : credentials.data!.length === 0 ? (
        !showForm && (
          <Empty title="No LLM keys yet">
            Add an OpenAI, Anthropic, or OpenAI-compatible (e.g. Ollama) key to start reviewing pull requests.
          </Empty>
        )
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2">Name</th>
                <th className="px-4 py-2">Provider</th>
                <th className="px-4 py-2">Default model</th>
                <th className="px-4 py-2">Key</th>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Used by</th>
                <th className="px-4 py-2">This month</th>
                <th className="px-4 py-2">Last validated</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {credentials.data!.map((c) => (
                <Fragment key={c.id}>
                  <tr>
                    <td className="px-4 py-2 font-medium">{c.name}</td>
                    <td className="px-4 py-2">
                      {providerLabel(c.provider)}
                      {c.base_url && <span className="block text-xs text-slate-500">{c.base_url}</span>}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs">{c.default_model}</td>
                    <td className="px-4 py-2 font-mono text-xs">{c.last4 ? `••••${c.last4}` : "—"}</td>
                    <td className="px-4 py-2">
                      <Badge tone={STATUS_TONE[c.status]}>{c.status}</Badge>
                      {c.status_message && <span className="block max-w-xs text-xs text-red-700">{c.status_message}</span>}
                    </td>
                    <td className="px-4 py-2">{c.in_use_by} repo(s)</td>
                    <td className="px-4 py-2">
                      <BudgetBar budget={c.budget} />
                      {c.status !== "revoked" && (
                        <button type="button" className="text-xs text-sky-700 hover:underline" onClick={() => setBudgeting(budgeting === c.id ? null : c.id)}>
                          {c.monthly_budget_usd ? "Change budget" : "Set budget"}
                        </button>
                      )}
                    </td>
                    <td className="px-4 py-2 text-slate-600">{formatDate(c.last_validated_at)}</td>
                    <td className="space-x-2 whitespace-nowrap px-4 py-2 text-right">
                      <Button variant="secondary" loading={validate.isPending && validate.variables === c.id} onClick={() => validate.mutate(c.id)}>
                        Validate
                      </Button>
                      {c.status !== "revoked" && (
                        <Button variant="secondary" onClick={() => setRotating(rotating === c.id ? null : c.id)}>
                          Rotate
                        </Button>
                      )}
                      <Button
                        variant="danger"
                        onClick={() => {
                          const warning = c.in_use_by > 0 ? ` ${c.in_use_by} enabled repo(s) use it and will stop being reviewed.` : "";
                          if (confirm(`Revoke "${c.name}"? The stored key is deleted.${warning}`)) revoke.mutate(c.id);
                        }}
                      >
                        Revoke
                      </Button>
                    </td>
                  </tr>
                  {budgeting === c.id && (
                    <tr>
                      <td colSpan={9} className="bg-slate-50 px-4 py-3">
                        <BudgetForm
                          credential={c}
                          onDone={() => {
                            setBudgeting(null);
                            invalidate();
                          }}
                          onCancel={() => setBudgeting(null)}
                        />
                      </td>
                    </tr>
                  )}
                  {rotating === c.id && (
                    <tr>
                      <td colSpan={9} className="bg-slate-50 px-4 py-3">
                        <RotateKeyForm
                          credential={c}
                          onDone={() => {
                            setRotating(null);
                            invalidate();
                          }}
                          onCancel={() => setRotating(null)}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="mt-8" id="prices">
        <ModelPrices providerLabel={providerLabel} />
      </div>
    </>
  );
}

function AddKeyForm({
  providers,
  onDone,
  onCancel,
}: {
  providers: LLMProviderInfo[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const [providerId, setProviderId] = useState(providers[0]?.id ?? "openai");
  const provider = providers.find((p) => p.id === providerId)!;
  const [form, setForm] = useState({ name: "", api_key: "", default_model: provider.suggested_models[0] ?? "", base_url: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      api<LLMCredential>("/llm-credentials", {
        method: "POST",
        body: {
          name: form.name || provider.label,
          provider: providerId,
          api_key: form.api_key,
          default_model: form.default_model,
          base_url: form.base_url,
        },
      }),
    onSuccess: onDone,
    onError: (err) => {
      if (err instanceof ApiError) {
        setErrors(err.fieldErrors());
        setError(err.message);
      } else setError(errorMessage(err));
    },
  });

  function changeProvider(id: string) {
    const next = providers.find((p) => p.id === id)!;
    setProviderId(id);
    setForm((f) => ({ ...f, default_model: next.suggested_models[0] ?? "", base_url: next.requires_base_url ? next.default_base_url : "" }));
  }

  return (
    <Card title="Add LLM key">
      <form
        className="grid gap-4 md:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          setErrors({});
          setError(null);
          create.mutate();
        }}
      >
        {error && (
          <div className="md:col-span-2">
            <Alert>{error}</Alert>
          </div>
        )}
        <Field label="Provider">
          <Select value={providerId} onChange={(e) => changeProvider(e.target.value)}>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Name" hint="Shown in the dashboard, e.g. “Team OpenAI”.">
          <Input value={form.name} placeholder={provider.label} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </Field>
        <Field label={provider.requires_api_key ? "API key" : "API key (optional)"} error={errors.api_key}>
          <Input
            type="password"
            autoComplete="off"
            required={provider.requires_api_key}
            value={form.api_key}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
          />
        </Field>
        <Field label="Default model" error={errors.default_model} hint={`Suggestions: ${provider.suggested_models.join(", ")}`}>
          <Input list="model-suggestions" required value={form.default_model} onChange={(e) => setForm({ ...form, default_model: e.target.value })} />
          <datalist id="model-suggestions">
            {provider.suggested_models.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </Field>
        {(provider.requires_base_url || providerId === "openai" || providerId === "anthropic") && (
          <Field
            label={provider.requires_base_url ? "Base URL" : "Base URL (optional)"}
            error={errors.base_url}
            hint={provider.requires_base_url ? "The OpenAI-compatible endpoint, ending in /v1." : "Only for proxies or gateways."}
          >
            <Input
              type="url"
              required={provider.requires_base_url}
              value={form.base_url}
              placeholder={provider.default_base_url}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            />
          </Field>
        )}
        <div className="flex gap-2 md:col-span-2">
          <Button type="submit" loading={create.isPending}>
            Validate &amp; save
          </Button>
          <Button type="button" variant="secondary" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}

function RotateKeyForm({ credential, onDone, onCancel }: { credential: LLMCredential; onDone: () => void; onCancel: () => void }) {
  const [apiKey, setApiKey] = useState("");
  const rotate = useMutation({
    mutationFn: () => api<LLMCredential>(`/llm-credentials/${credential.id}/rotate`, { method: "POST", body: { api_key: apiKey } }),
    onSuccess: onDone,
  });
  const fieldError = rotate.error instanceof ApiError ? rotate.error.fieldErrors().api_key : undefined;
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        rotate.mutate();
      }}
    >
      <div className="min-w-72 flex-1">
        <Field
          label={`New key for "${credential.name}"`}
          hint="Validated before it replaces the stored key. Repositories using this key keep working."
          error={fieldError ?? (rotate.error && !fieldError ? errorMessage(rotate.error) : undefined)}
        >
          <Input type="password" autoComplete="off" required value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
        </Field>
      </div>
      <Button type="submit" loading={rotate.isPending}>
        Validate and replace
      </Button>
      <Button type="button" variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
    </form>
  );
}

function BudgetForm({ credential, onDone, onCancel }: { credential: LLMCredential; onDone: () => void; onCancel: () => void }) {
  const [value, setValue] = useState(credential.monthly_budget_usd ?? "");
  const save = useMutation({
    mutationFn: (budget: string | null) =>
      api<LLMCredential>(`/llm-credentials/${credential.id}`, { method: "PATCH", body: { monthly_budget_usd: budget } }),
    onSuccess: onDone,
  });
  const fieldError = save.error instanceof ApiError ? save.error.fieldErrors().monthly_budget_usd : undefined;
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate(value.trim() ? value.trim() : null);
      }}
    >
      <div className="w-64">
        <Field
          label={`Monthly budget for "${credential.name}" (USD)`}
          hint="Admins are alerted at 80%. At 100% automatic reviews pause until the next month."
          error={fieldError ?? (save.error && !fieldError ? errorMessage(save.error) : undefined)}
        >
          <Input type="number" min="0.01" step="0.01" value={value} placeholder="No budget" onChange={(e) => setValue(e.target.value)} />
        </Field>
      </div>
      <Button type="submit" loading={save.isPending}>
        Save
      </Button>
      {credential.monthly_budget_usd && (
        <Button type="button" variant="secondary" onClick={() => save.mutate(null)}>
          Remove budget
        </Button>
      )}
      <Button type="button" variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
    </form>
  );
}

const EMPTY_PRICE = { provider: "", model_prefix: "", input_usd_per_mtok: "", output_usd_per_mtok: "" };

function ModelPrices({ providerLabel }: { providerLabel: (id: string) => string }) {
  const queryClient = useQueryClient();
  const prices = useQuery({ queryKey: ["model-prices"], queryFn: () => api<ModelPrice[]>("/model-prices") });
  const providers = useQuery({ queryKey: ["llm-providers"], queryFn: () => api<LLMProviderInfo[]>("/llm-providers") });
  const [draft, setDraft] = useState(EMPTY_PRICE);
  const [editing, setEditing] = useState<ModelPrice | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["model-prices"] });
  const save = useMutation({
    mutationFn: (body: typeof EMPTY_PRICE & { id?: number }) =>
      body.id
        ? api<ModelPrice>(`/model-prices/${body.id}`, { method: "PATCH", body })
        : api<ModelPrice>("/model-prices", { method: "POST", body }),
    onSuccess: () => {
      setDraft(EMPTY_PRICE);
      setEditing(null);
      refresh();
    },
  });
  const remove = useMutation({ mutationFn: (id: number) => api(`/model-prices/${id}`, { method: "DELETE" }), onSuccess: refresh });
  const recalc = useMutation({
    mutationFn: () => api<{ updated: number; unpriced_remaining: number }>("/model-prices/recalculate", { method: "POST" }),
    onSuccess: (r) => {
      setMessage(`Filled in the cost of ${r.updated} past LLM calls. ${r.unpriced_remaining} still have no price.`);
      queryClient.invalidateQueries({ queryKey: ["llm-credentials"] });
    },
  });
  const restore = useMutation({
    mutationFn: () => api<{ added: number }>("/model-prices/defaults", { method: "POST" }),
    onSuccess: (r) => {
      setMessage(`Restored ${r.added} missing default prices.`);
      refresh();
    },
  });
  const errors = save.error instanceof ApiError ? save.error.fieldErrors() : {};

  const form = (value: typeof EMPTY_PRICE, set: (v: typeof EMPTY_PRICE) => void, id?: number) => (
    <form
      className="grid items-end gap-2 md:grid-cols-[1fr_1.5fr_1fr_1fr_auto]"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate({ ...value, id });
      }}
    >
      <Field label="Provider" error={errors.provider}>
        <Select value={value.provider} onChange={(e) => set({ ...value, provider: e.target.value })}>
          <option value="">Any provider</option>
          {providers.data?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Model name starts with" error={errors.model_prefix}>
        <Input value={value.model_prefix} placeholder="e.g. gpt-4o-mini (empty = all models)" onChange={(e) => set({ ...value, model_prefix: e.target.value })} />
      </Field>
      <Field label="Input $ / 1M tokens" error={errors.input_usd_per_mtok}>
        <Input type="number" min="0" step="0.0001" required value={value.input_usd_per_mtok} onChange={(e) => set({ ...value, input_usd_per_mtok: e.target.value })} />
      </Field>
      <Field label="Output $ / 1M tokens" error={errors.output_usd_per_mtok}>
        <Input type="number" min="0" step="0.0001" required value={value.output_usd_per_mtok} onChange={(e) => set({ ...value, output_usd_per_mtok: e.target.value })} />
      </Field>
      <div className="flex gap-2">
        <Button type="submit" loading={save.isPending}>
          {id ? "Save" : "Add"}
        </Button>
        {id && (
          <Button type="button" variant="ghost" onClick={() => setEditing(null)}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  );

  return (
    <Card
      title="Model prices"
      actions={
        <>
          <Button variant="secondary" loading={restore.isPending} onClick={() => restore.mutate()}>
            Restore defaults
          </Button>
          <Button variant="secondary" loading={recalc.isPending} onClick={() => recalc.mutate()}>
            Recalculate unpriced usage
          </Button>
        </>
      }
    >
      <p className="mb-3 text-sm text-slate-600">
        Used to compute review cost and enforce budgets. Defaults are list prices for hosted models; providers change
        them, so check them against your contract. Price self-hosted models at 0. The longest matching prefix wins.
      </p>
      {message && (
        <div className="mb-3">
          <Alert kind="success">{message}</Alert>
        </div>
      )}
      {(save.error && Object.keys(errors).length === 0) || remove.error ? (
        <div className="mb-3">
          <Alert>{errorMessage(save.error ?? remove.error)}</Alert>
        </div>
      ) : null}
      {prices.isLoading ? (
        <Spinner />
      ) : (
        <table className="mb-4 min-w-full divide-y divide-slate-200 text-sm">
          <thead className="text-left text-xs font-medium uppercase tracking-wide text-slate-500">
            <tr>
              <th className="py-1 pr-3">Provider</th>
              <th className="py-1 pr-3">Model prefix</th>
              <th className="py-1 pr-3 text-right">Input / 1M</th>
              <th className="py-1 pr-3 text-right">Output / 1M</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {prices.data?.map((p) =>
              editing?.id === p.id ? (
                <tr key={p.id}>
                  <td colSpan={5} className="py-2">
                    {form(
                      { provider: editing.provider, model_prefix: editing.model_prefix, input_usd_per_mtok: editing.input_usd_per_mtok, output_usd_per_mtok: editing.output_usd_per_mtok },
                      (v) => setEditing({ ...editing, ...v }),
                      p.id,
                    )}
                  </td>
                </tr>
              ) : (
                <tr key={p.id}>
                  <td className="py-1 pr-3">{p.provider ? providerLabel(p.provider) : <span className="text-slate-500">any</span>}</td>
                  <td className="py-1 pr-3 font-mono text-xs">
                    {p.model_prefix || <span className="text-slate-500">all models</span>} {!p.is_default && <Badge tone="sky">custom</Badge>}
                  </td>
                  <td className="py-1 pr-3 text-right">${Number(p.input_usd_per_mtok).toFixed(2)}</td>
                  <td className="py-1 pr-3 text-right">${Number(p.output_usd_per_mtok).toFixed(2)}</td>
                  <td className="space-x-2 whitespace-nowrap py-1 text-right">
                    <Button variant="ghost" className="px-2 py-1 text-xs" onClick={() => setEditing(p)}>
                      Edit
                    </Button>
                    <Button
                      variant="ghost"
                      className="px-2 py-1 text-xs"
                      onClick={() => {
                        if (confirm(`Delete the price for ${p.model_prefix || "all models"}? Future calls to matching models will have no cost.`)) remove.mutate(p.id);
                      }}
                    >
                      Delete
                    </Button>
                  </td>
                </tr>
              ),
            )}
          </tbody>
        </table>
      )}
      <h3 className="mb-2 text-sm font-medium">Add a price</h3>
      {form(draft, setDraft)}
    </Card>
  );
}

