// Phase 1 REST/SSE payload contract of the local web dashboard
// (docs/design-local-web-ui.md §6). The backend DTO golden JSON matches these
// shapes; every state enum is a closed union so an unknown value is a type error.

/** 32 lowercase hex characters: the install state root key. */
export type InstallId = string;
/** `<installId>` for a main checkout, `<installId>:<wtHash12>` for a linked worktree. */
export type TargetId = string;
/** ISO-8601 timestamp as the server wrote it. */
export type IsoTime = string;

export type HostName = 'claude' | 'codex' | 'hermes' | 'gemini';
export type SloVerdict = 'ok' | 'over' | 'no-data';
export type TabId = 'overview' | 'wiki' | 'recall' | 'runtime';

export interface ApiErrorBody {
  error: string;
  code: string;
}

// ---------------------------------------------------------------- session

export interface UiPreferences {
  schema: string;
  autoOpenOnSessionStart: boolean;
  autoOpenScope: 'onboarded_sessions';
  idleShutdownMinutes: number;
  autoOpenCooldownMinutes: number;
  reconnectGraceSeconds: number;
  readOnlyDefault: boolean;
}

export interface SessionMeta {
  bundleVersion: string;
  activeClients: number;
  /** True when the server runs from an installed plugin cache, false for a development checkout. */
  isInstalledPluginCache: boolean;
  readOnly: boolean;
  idleSecondsRemaining: number | null;
  preferences: UiPreferences;
}

export interface AuthExchangeResponse {
  ok: true;
}

// ---------------------------------------------------------------- overview

export type SnapshotState = 'complete' | 'stale' | 'absent' | 'busy' | 'recovery_required';

export interface CardCounts {
  verified: number;
  generated: number;
  pending: number;
}

/** `<name>`, `detached@<short7>`, `non-git` or `unknown` (`<gitdir>/HEAD` of the nearest `.git` at or above the root). */
export type BranchLabel = string;

/**
 * Checkout state from the registry row and the route proof: `ready` (active, proved),
 * `view_preparing` (cold worktree view), `proof_failed` (root present, route re-proof failed),
 * `path_missing` (`missing_pending`, `detached/missing`, or an active row whose root is gone),
 * `released` (`detached/released`, `detached/withdrawn`), `moved` (`detached/root_moved`),
 * `conflict` (two installs claim the root).
 */
export type CheckoutViewState =
  | 'ready'
  | 'view_preparing'
  | 'proof_failed'
  | 'path_missing'
  | 'released'
  | 'moved'
  | 'conflict';

interface CheckoutNodeBase {
  targetId: TargetId;
  installId: InstallId;
  name: string;
  displayRoot: string;
  /** `unknown` for a row that is not active or whose root is gone. */
  branch: BranchLabel;
  viewState: CheckoutViewState;
  /** Reason code of a failed route proof (`proof_failed` only), else null. */
  proofReason: string | null;
  hosts: HostName[];
  cards: CardCounts;
  /** Last published `wake.sync`, or null when none was seen. */
  lastSyncAt: IsoTime | null;
  recalls24h: number;
  p50Ms: number | null;
  p95Ms: number | null;
  slo: SloVerdict;
}

export interface MainCheckoutNode extends CheckoutNodeBase {
  kind: 'main';
}

export interface LinkedWorktreeNode extends CheckoutNodeBase {
  kind: 'worktree';
  parentTargetId: TargetId;
}

export type WorktreeNode = MainCheckoutNode | LinkedWorktreeNode;

export interface ProjectGroup {
  installId: InstallId;
  main: MainCheckoutNode;
  worktrees: LinkedWorktreeNode[];
}

export type AlertSeverity = 'info' | 'warn' | 'error';

export type AlertScope =
  | {kind: 'global'}
  | {kind: 'install'; installId: InstallId}
  | {kind: 'target'; targetId: TargetId};

export interface AlertBannerItem {
  /** Machine code, e.g. `needs_extractor`, `laya_vector_selected_provider_start_failed`, `view_preparing`. */
  code: string;
  severity: AlertSeverity;
  scope: AlertScope;
  message: string;
  /** Seconds until an automatic retry (e.g. `attestation_invalid` re-proof), when one is scheduled. */
  retryInSeconds: number | null;
}

export interface InstallSummary {
  installId: InstallId;
  projectName: string;
  recalls24h: number;
  p50Ms: number | null;
  p95Ms: number | null;
  slo: SloVerdict;
  errors24h: number;
  lastErrorAt: IsoTime | null;
}

export interface SnapshotMeta {
  state: SnapshotState;
  truncated: boolean;
  maxRows: number;
  lastReconcileAt: IsoTime | null;
}

export interface OverviewResponse {
  generatedAt: IsoTime;
  snapshot: SnapshotMeta;
  projects: ProjectGroup[];
  installs: InstallSummary[];
  alerts: AlertBannerItem[];
  /** Metadata only; `diagnostic.part` details load through the errors endpoint. */
  recentErrors: ErrorEventItem[];
}

// ---------------------------------------------------------------- events

export type RecallOutcome = 'served' | 'empty' | 'refused';
export type SemanticStatus = 'complete' | 'partial' | 'lexical_only' | 'provider_absent';

export interface RecallStageTimings {
  pinMs: number | null;
  lexicalMs: number | null;
  stillCurrentMs: number | null;
  checkoutMs: number | null;
  queryEmbeddingMs: number | null;
  semanticMs: number | null;
  selectorResidentSeconds: number | null;
}

export interface InjectedCard {
  path: string;
  sha: string;
}

export interface RecallEventItem {
  ts: IsoTime;
  event: 'recall' | 'recall.edit';
  installId: InstallId;
  targetId: TargetId | null;
  host: HostName;
  session: string;
  outcome: RecallOutcome;
  reason: string;
  notes: string[];
  semantic: SemanticStatus | null;
  vectorCoveredMembers: number | null;
  vectorEligibleMembers: number | null;
  degraded: ('worktree' | 'reader_endpoint' | 'preview')[];
  cards: InjectedCard[];
  prompt: {sha256: string; chars: number};
  timings: RecallStageTimings;
  totalMs: number | null;
}

export interface RecallsResponse {
  installId: InstallId;
  hours: number;
  items: RecallEventItem[];
}

export interface ErrorEventItem {
  ts: IsoTime;
  installId: InstallId;
  targetId: TargetId | null;
  host: HostName | null;
  event: string;
  reason: string;
  /** Diagnostic id whose parts the details request reassembles; null when the event has none. */
  diagId: string | null;
  /** Present only in a `details=1` response. */
  detail?: string;
}

export interface ErrorsResponse {
  installId: InstallId;
  hours: number;
  details: boolean;
  items: ErrorEventItem[];
}

// ---------------------------------------------------------------- wiki

export type WikiCardStatus = 'verified' | 'generated';
export type WikiListFilter = 'all' | 'verified' | 'generated' | 'pending' | 'stale' | 'hidden' | 'over_budget';

export type WikiStaleReason = 'source_deleted' | 'source_changed';

export interface WikiCardMeta {
  rel: string;
  title: string;
  status: WikiCardStatus;
  tokens: number;
  overBudget: boolean;
  stale: boolean;
  /** Why a stale card is stale: a source file is gone, or a source's bytes changed. */
  staleReason: WikiStaleReason | null;
  hidden: boolean;
  updatedAt: IsoTime | null;
}

export interface WikiLedgerSummary {
  compileJobs: number;
  verifyJobs: number;
  publishTxns: number;
  refreshEvents: number;
  dedupPairs: number;
}

export interface WikiListResponse {
  targetId: TargetId;
  /** Linked worktrees read compile/backfill ledgers from their main checkout. */
  ledgerFrom: TargetId;
  cards: WikiCardMeta[];
  truncated: boolean;
  ledger: WikiLedgerSummary;
}

export interface SourceSpan {
  path: string;
  startLine: number;
  endLine: number;
}

export type WikiBlock =
  | {kind: 'auto'; markdown: string}
  | {kind: 'manual'; markdown: string};

export interface WikiCardDetail {
  targetId: TargetId;
  rel: string;
  frontmatter: {
    status: WikiCardStatus;
    createdBy: string | null;
    sourceRevisions: Record<string, string>;
    sourceSpans: SourceSpan[];
    related: string[];
    aliases: string[];
  };
  blocks: WikiBlock[];
}

export interface WikiSourceResponse {
  targetId: TargetId;
  rel: string;
  lines: string[];
  highlights: {startLine: number; endLine: number}[];
  truncated: boolean;
}

// ---------------------------------------------------------------- query playground

export interface LexicalRow {
  path: string;
  blob: string;
  score: number;
  rank: number;
}

export interface SemanticRow {
  path: string;
  blob: string;
  cosine: number;
  rank: number;
}

export interface FusedRow {
  path: string;
  blob: string;
  rrfScore: number;
  rank: number;
}

export interface SelectorDecision {
  path: string;
  picked: boolean;
  reason: string;
}

export interface SelectorResult {
  ran: boolean;
  reason: string | null;
  checkpoint: string | null;
  final: InjectedCard[];
  decisions: SelectorDecision[];
}

export interface DryRunRecallResponse {
  route: string;
  linked: boolean;
  outcome: RecallOutcome;
  reason: string;
  generation: string | null;
  lexicalLists: {query: string; rows: LexicalRow[]}[];
  semanticList: SemanticRow[] | null;
  fusedList: FusedRow[];
  selectorResult: SelectorResult;
  semanticStatus: SemanticStatus | string | null;
  vectorCoveredMembers: number | null;
  vectorEligibleMembers: number | null;
  fallbackReason: string | null;
  notes: string[];
  timings: Record<string, number>;
}

// ---------------------------------------------------------------- runtime & learning

export type WakeState = 'running' | 'waiting' | 'deferred_for_resources';

export interface WakeQueueItem {
  kind: string;
  /** User-language label, e.g. `색인 동기화 (laya-sync)`. */
  label: string;
  installId: InstallId | null;
  projectName: string | null;
  state: WakeState;
  attempt: number;
  dueAt: IsoTime | null;
  deferrals: number;
}

export interface SharedWorkerState {
  socket: string;
  running: boolean;
  pid: number | null;
  bundleVersion: string | null;
  accelerator: string | null;
}

export interface ProviderProofState {
  installId: InstallId;
  state: 'proved' | 'attestation_invalid' | 'unavailable';
  reproofInSeconds: number | null;
}

export interface DiskUsageItem {
  label: string;
  bytes: number;
}

export interface RuntimeResponse {
  sharedWorkers: SharedWorkerState[];
  proofs: ProviderProofState[];
  wakes: WakeQueueItem[];
  disk: DiskUsageItem[];
}

export interface PromotionGate {
  code: 'candidateTrain' | 'candidateEval' | 'candidateEvalNonEmpty' | 'candidateEvalEmpty';
  current: number;
  required: number;
}

export interface LearningResponse {
  installId: InstallId;
  checkpoint: string | null;
  gates: PromotionGate[];
  caseRows: number;
  caseRowsCap: number;
  metrics: {exactRate: number | null; microF1: number | null; rankExactRate: number | null};
  teacher: {host: HostName; callsToday: number; waitingReason: string | null}[];
}

// ---------------------------------------------------------------- SSE

export type DrainReason = 'bundle_upgraded' | 'user_stop' | 'idle';

export type SseEvent =
  | {type: 'heartbeat'; data: {ts: IsoTime; activeClients: number; idleSecondsRemaining: number | null}}
  | {type: 'session_started'; data: {targetId: TargetId | null; installId: InstallId | null; host: HostName | null; ts: IsoTime}}
  | {type: 'events_updated'; data: {installIds: InstallId[]}}
  | {type: 'runtime_updated'; data: {ts: IsoTime}}
  | {type: 'server_draining'; data: {reason: DrainReason}};

export type SseEventType = SseEvent['type'];
