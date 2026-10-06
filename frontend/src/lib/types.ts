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
  is_active: boolean;
  github_login: string;
  has_password: boolean;
  last_login: string | null;
  created_at: string;
}

export const ROLES: { id: Role; label: string; description: string }[] = [
  { id: "admin", label: "Admin", description: "Everything, including keys, integrations, users and the audit log." },
  { id: "reviewer", label: "Reviewer", description: "Re-run reviews, triage findings, edit review rules." },
  { id: "viewer", label: "Viewer", description: "Read-only access to pull requests and reviews." },
];

export type InviteStatus = "pending" | "accepted" | "revoked" | "expired";

export interface Invite {
  id: number;
  email: string;
  role: Role;
  status: InviteStatus;
  created_by_email: string;
  created_at: string;
  expires_at: string;
  accepted_at: string | null;
}

export interface CreatedInvite extends Invite {
  url: string;
  email_sent: boolean;
}

export interface LoginProvider {
  id: string;
  name: string;
  start_url: string;
}

export type PasswordLogin = "all" | "admins" | "none";

export interface SetupStatus {
  needs_setup: boolean;
  github_login: boolean;
  login_providers: LoginProvider[];
  password_login: PasswordLogin;
}

export interface InviteInfo {
  email: string;
  role: Role;
  expires_at: string;
  github_login: boolean;
  login_providers: LoginProvider[];
  password_login: PasswordLogin;
}

export interface ExternalIdentity {
  id: number;
  provider: string;
  provider_name: string;
  username: string;
  email: string;
  created_at: string;
  last_login_at: string | null;
}

export interface AuditEvent {
  id: number;
  created_at: string;
  actor: number | null;
  actor_email: string;
  action: string;
  target_type: string;
  target_id: string;
  target_label: string;
  ip: string | null;
  user_agent: string;
  metadata: Record<string, unknown>;
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
  monthly_budget_usd: string | null;
  budget: BudgetStatus;
}

export interface BudgetStatus {
  budget_usd: string | null;
  spent_usd: string;
  percent: number | null;
  state: "none" | "ok" | "warning" | "exceeded";
  unpriced_requests: number;
}

export interface ModelPrice {
  id: number;
  provider: string;
  model_prefix: string;
  input_usd_per_mtok: string;
  output_usd_per_mtok: string;
  is_default: boolean;
  updated_at: string;
}

export interface UsageRow {
  day?: string;
  repository_id?: number | null;
  repository__full_name?: string | null;
  credential_id?: number | null;
  credential__name?: string | null;
  model?: string;
  requests: number;
  errors: number;
  reviews: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: string | number | null;
  unpriced: number;
}

export interface UsageReport {
  from: string;
  to: string;
  group_by: string[];
  rows: UsageRow[];
  totals: Omit<UsageRow, "day" | "repository_id" | "repository__full_name" | "credential_id" | "credential__name" | "model">;
  prices_as_of: string;
}

export interface ApiToken {
  id: number;
  name: string;
  hint: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  active: boolean;
}

export function formatUsd(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n > 0 && n < 0.01 && digits === 2) return "< $0.01";
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
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
  suggest_tests: boolean;
  extended_context: boolean;
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
  provider: GitProviderId;
  webhook_managed: boolean | null;
  webhook_warning?: string;
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
  risk_score: number | null;
}

export interface PullRequest {
  id: number;
  repository: { id: number; full_name: string; provider: GitProviderId };
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
  state: FindingState;
  dismiss_reason: DismissReason | "";
  state_changed_at: string | null;
  votes: { up: number; down: number; mine: "up" | "down" | null };
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
  cost_usd: string | null;
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

export type FindingState = "open" | "accepted" | "dismissed";
export type DismissReason = "false_positive" | "wont_fix" | "duplicate" | "other";

export interface RunComparison {
  run: number;
  with: number;
  added: Finding[];
  resolved: Finding[];
  unchanged: number;
}

export interface FeedbackStat {
  category: Category;
  reported: number;
  accepted: number;
  dismissed: number;
  false_positive: number;
  up: number;
  down: number;
  acceptance_rate: number | null;
}

export function riskBucket(score: number | null | undefined): "low" | "medium" | "high" | null {
  if (score === null || score === undefined) return null;
  return score >= 60 ? "high" : score >= 30 ? "medium" : "low";
}

/** Repository settings a reviewer may change (mirrors REVIEWER_SETTINGS_FIELDS in the API). */
export const REVIEWER_SETTINGS_FIELDS: string[] = [
  "profile",
  "min_severity",
  "min_confidence",
  "max_inline_comments",
  "ignore_patterns",
  "replace_default_ignores",
  "custom_instructions",
  "rules",
  "suggest_tests",
  "extended_context",
  "post_when_no_findings",
];

export type NotificationKind = "slack" | "email" | "webhook";

export interface NotificationChannel {
  id: number;
  name: string;
  kind: NotificationKind;
  enabled: boolean;
  url_hint: string;
  has_secret: boolean;
  recipients: string[];
  events: string[];
  min_risk: number;
  min_severity: Severity | "";
  repositories: number[];
  created_at: string;
  updated_at: string;
}

export interface NotificationDelivery {
  id: number;
  event: string;
  title: string;
  status: "pending" | "sent" | "failed";
  error: string;
  attempts: number;
  created_at: string;
  sent_at: string | null;
}

export const NOTIFICATION_EVENTS: { id: string; label: string; hint: string }[] = [
  { id: "review.high_risk", label: "High-risk pull requests", hint: "Risk score or a finding reaches the thresholds below." },
  { id: "review.failed", label: "Failed reviews", hint: "A review could not finish (LLM errors, invalid key, GitHub errors)." },
  { id: "budget.threshold", label: "LLM budget alerts", hint: "A key reaches 80% or 100% of its monthly budget." },
  { id: "digest.weekly", label: "Weekly digest", hint: "Mondays: reviews, findings, triage, cost and the riskiest PRs." },
];

export type GitProviderId = "github" | "gitlab" | "bitbucket";

export interface GitLabIntegration {
  connected: boolean;
  webhook_url: string;
  web_url?: string;
  username?: string;
  webhook_secret?: string;
  repositories?: number;
}

/** "#12" on GitHub, "!12" for GitLab merge requests. */
export function prRef(provider: GitProviderId | undefined, number: number): string {
  return provider === "gitlab" ? `!${number}` : `#${number}`;
}

export const PROVIDER_LABEL: Record<GitProviderId, string> = { github: "GitHub", gitlab: "GitLab", bitbucket: "Bitbucket" };

