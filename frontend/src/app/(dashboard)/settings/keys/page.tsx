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
import type { LLMCredential, LLMProviderInfo } from "@/lib/types";

const STATUS_TONE = { valid: "green", invalid: "red", revoked: "slate" } as const;

export default function KeysPage() {
  const queryClient = useQueryClient();
  const credentials = useQuery({ queryKey: ["llm-credentials"], queryFn: () => api<LLMCredential[]>("/llm-credentials") });
  const providers = useQuery({ queryKey: ["llm-providers"], queryFn: () => api<LLMProviderInfo[]>("/llm-providers") });
  const [showForm, setShowForm] = useState(false);
  const [rotating, setRotating] = useState<number | null>(null);

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
                  {rotating === c.id && (
                    <tr>
                      <td colSpan={8} className="bg-slate-50 px-4 py-3">
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
