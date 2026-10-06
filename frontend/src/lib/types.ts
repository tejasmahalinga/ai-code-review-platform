// Types mirroring the REST API (see GET /api/v1/schema/ for the OpenAPI document).

export type Role = "admin" | "reviewer" | "viewer";
export type Severity = "critical" | "high" | "medium" | "low" | "info";
export type Category = "bug" | "security" | "performance" | "maintainability" | "style" | "test";

export const SEVERITIES: Severity[] = ["critical", "high", "medium", "low", "info"];

export interface ReviewRule {
  id: string;
  description: string;
  severity: Severity;
  paths: string[];
  enabled: boolean;
}

export type ProfileId = "strict" | "balanced" | "lenient" | "security";

export interface ReviewProfile {
  id: ProfileId;
  label: string;
  description: string;
  categories: Category[] | null;
  defaults: { min_severity: Severity; min_confidence: number; max_inline_comments: number };
}

export interface User {
  id: number;
  email: string;
  name: string;
  role: Role;
  created_at: string;
}

export interface Paginated<T> {
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface LLMProviderInfo {
  id: string;
  label: string;
  requires_api_key: boolean;
  requires_base_url: boolean;
  suggested_models: string[];
  default_base_url: string;
}

export interface LLMCredential {
  id: number;
  name: string;
  provider: string;
  base_url: string;
  last4: string;
  default_model: string;
  status: "valid" | "invalid" | "revoked";
  status_message: string;
  last_validated_at: string | null;
  created_at: string;
  revoked_at: string | null;
  in_use_by: number;
}

export interface Installation {
  id: number;
  account_login: string;
  account_type: string;
  suspended: boolean;
  repository_count: number;
}

export interface GitHubIntegration {
  connected: boolean;
  web_url: string;
  app_name: string;
  app_slug: string;
  app_html_url: string;
  install_url: string;
  webhook_url: string;
  installations: Installation[];
}

export interface GitHubManifest {
  post_url: string;
  manifest: string;
  state: string;
}

export interface RepositorySettings {
  auto_review: boolean;
  review_drafts: boolean;
  credential: number | null;
  model: string;
  effective_model: string;
  ignore_patterns: string[];
  replace_default_ignores: boolean;
  default_ignore_patterns: string[];
  custom_instructions: string;
  min_severity: Severity;
  min_confidence: number;
  max_inline_comments: number;
  max_changed_lines: number;
  max_files: number;
  max_input_tokens: number;
  chunk_tokens: number;
  post_when_no_findings: boolean;
  profile: ProfileId;
  review_on_push: boolean;
  check_runs: boolean;
  gate_severity: Severity | "";
  base_branch_patterns: string[];
  rules: ReviewRule[];
  updated_at: string;
}

export interface Repository {
  id: number;
  full_name: string;
  private: boolean;
  html_url: string;
  default_branch: string;
  status: "active" | "removed";
  enabled: boolean;
  installation_account: string;
  credential_name: string | null;
  model: string;
  auto_review: boolean;
  pull_request_count: number;
}

export type ReviewStatus = "queued" | "running" | "completed" | "failed" | "skipped" | "cancelled";

export type SeverityCounts = Record<Severity, number>;

export interface ReviewRunSummary {
  id: number;
  status: ReviewStatus;
  status_reason: string;
  trigger: "webhook" | "manual" | "push" | "command";
  head_sha: string;
  created_at: string;
  finished_at: string | null;
  counts: SeverityCounts;
  posted_count: number;
}

export interface PullRequest {
  id: number;
  repository: { id: number; full_name: string };
  number: number;
  title: string;
  author_login: string;
  state: "open" | "closed" | "merged";
  is_draft: boolean;
  html_url: string;
  head_sha: string;
  updated_at: string;
  reviews_paused: boolean;
  latest_review: ReviewRunSummary | null;
}

export interface Finding {
  id: number;
  path: string;
  line_start: number | null;
  line_end: number | null;
  anchored: boolean;
  category: Category;
  severity: Severity;
  confidence: number;
  title: string;
  body: string;
  suggestion: string;
  post_status: string;
  post_status_label: string;
  provider_comment_url: string;
  fingerprint: string;
  rule_id: string;
}

export interface IgnoredFile {
  path: string;
  reason: string;
  pattern: string | null;
}

export interface ReviewedFile {
  path: string;
  status: string;
  additions: number;
  deletions: number;
}

export interface ReviewRun extends ReviewRunSummary {
  pull_request: PullRequest;
  stage: string;
  stage_label: string;
  error: string;
  base_sha: string;
  model: string;
  credential_name: string | null;
  summary: string;
  input_tokens: number;
  output_tokens: number;
  chunk_count: number;
  chunks_failed: number;
  duration_ms: number | null;
  started_at: string | null;
  provider_review_url: string;
  files_reviewed: ReviewedFile[];
  files_ignored: IgnoredFile[];
  findings: Finding[];
  created_by_email: string | null;
  incremental: boolean;
  compare_base_sha: string;
  profile: ProfileId;
  config_source: string;
  config_error: string;
}

export interface WebhookDelivery {
  id: number;
  provider: string;
  delivery_id: string;
  event: string;
  action: string;
  repository: string | null;
  status: string;
  reason: string;
  review_run: number | null;
  received_at: string;
}
