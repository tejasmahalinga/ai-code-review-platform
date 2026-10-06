import { cx } from "@/components/ui";
import type { BudgetStatus } from "@/lib/types";
import { formatUsd } from "@/lib/types";

const BUDGET_TONE: Record<BudgetStatus["state"], string> = {
  none: "bg-slate-300",
  ok: "bg-emerald-500",
  warning: "bg-amber-500",
  exceeded: "bg-red-500",
};

export function BudgetBar({ budget }: { budget: BudgetStatus }) {
  if (budget.state === "none") return <span className="text-xs text-slate-500">{formatUsd(budget.spent_usd)} this month · no budget</span>;
  const width = Math.min(budget.percent ?? 0, 100);
  return (
    <div className="min-w-40">
      <div className="h-2 overflow-hidden rounded bg-slate-100" role="progressbar" aria-valuenow={budget.percent ?? 0} aria-valuemin={0} aria-valuemax={100}>
        <div className={cx("h-2", BUDGET_TONE[budget.state])} style={{ width: `${width}%` }} />
      </div>
      <div className="mt-1 text-xs text-slate-600">
        {formatUsd(budget.spent_usd)} of {formatUsd(budget.budget_usd)} this month ({budget.percent?.toFixed(0)}%)
        {budget.state === "exceeded" && <span className="ml-1 font-medium text-red-700">· reviews paused</span>}
      </div>
    </div>
  );
}
