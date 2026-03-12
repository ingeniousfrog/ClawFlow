"""Audit logging for all agent actions.
记录关键安全与执行事件，支持追溯、统计、完整性校验。
Notes：若 key 与 DB 同时泄露，可信度会下降。"""

from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

from nanoclaw.core.logger import get_logger

logger = get_logger(__name__)


def _get_hmac_key() -> bytes:
    """
    生成并持久化审计密钥。
    Get or create a persistent HMAC key for audit log integrity.

    The key is stored in the config directory alongside the database.
    """
    from nanoclaw.core.config import get_data_path

    key_path = get_data_path() / ".audit_hmac_key"
    if key_path.exists():
        return key_path.read_bytes()
    key = uuid.uuid4().bytes + uuid.uuid4().bytes  # 32 bytes
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key)
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    return key


class AuditLog:
    """Immutable log of every agent action with HMAC integrity."""

    def __init__(self, db_path: str | Path):
        """
        Initialize AuditLog.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._hmac_key = _get_hmac_key()
        self._init_db()

    def _init_db(self) -> None:
        """Create audit log table if not exists."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                session_id TEXT,
                action_type TEXT NOT NULL,
                tool_name TEXT,
                input_summary TEXT,
                output_summary TEXT,
                status TEXT NOT NULL DEFAULT 'success',
                tokens_used INTEGER DEFAULT 0,
                execution_ms INTEGER DEFAULT 0,
                integrity_hash TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                session_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                workflow_identity TEXT NOT NULL DEFAULT '',
                workflow_tags TEXT DEFAULT '[]',
                user_summary TEXT,
                status TEXT NOT NULL DEFAULT 'success',
                failure_reason TEXT DEFAULT '',
                total_tokens INTEGER DEFAULT 0,
                execution_ms INTEGER DEFAULT 0,
                llm_calls INTEGER DEFAULT 0,
                tool_calls INTEGER DEFAULT 0,
                final_model TEXT DEFAULT '',
                call_chain TEXT DEFAULT '[]',
                integrity_hash TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                workflow_run_id INTEGER NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                workflow_status TEXT NOT NULL DEFAULT '',
                quality_score INTEGER NOT NULL DEFAULT 0,
                efficiency_score INTEGER NOT NULL DEFAULT 0,
                feedback_signal TEXT NOT NULL DEFAULT 'unknown',
                evaluation_label TEXT NOT NULL DEFAULT 'review',
                suggestions_json TEXT NOT NULL DEFAULT '[]',
                failure_classes_json TEXT NOT NULL DEFAULT '[]',
                attention_reasons_json TEXT NOT NULL DEFAULT '[]',
                follow_up_actions_json TEXT NOT NULL DEFAULT '[]',
                integrity_hash TEXT,
                FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provider_usage (
                provider_name TEXT PRIMARY KEY,
                used_calls INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'success',
                failure_reason TEXT NOT NULL DEFAULT '',
                final_output_summary TEXT NOT NULL DEFAULT '',
                execution_ms INTEGER DEFAULT 0,
                integrity_hash TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                step_id TEXT NOT NULL DEFAULT '',
                attempt_number INTEGER NOT NULL DEFAULT 0,
                tool_name TEXT NOT NULL,
                input_summary TEXT NOT NULL DEFAULT '',
                output_summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'success',
                execution_ms INTEGER DEFAULT 0,
                cached INTEGER NOT NULL DEFAULT 0,
                integrity_hash TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_timestamp ON workflow_runs(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_eval_timestamp "
            "ON workflow_evaluations(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, timestamp DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_traces_task ON tool_traces(task_id, timestamp DESC)"
        )
        # Add integrity_hash column to existing databases
        try:
            conn.execute("ALTER TABLE audit_log ADD COLUMN integrity_hash TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE workflow_runs ADD COLUMN integrity_hash TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE workflow_runs ADD COLUMN workflow_identity TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_identity "
            "ON workflow_runs(workflow_name, workflow_identity, timestamp DESC)"
        )
        try:
            conn.execute("ALTER TABLE workflow_evaluations ADD COLUMN integrity_hash TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE workflow_evaluations "
                "ADD COLUMN failure_classes_json TEXT NOT NULL DEFAULT '[]'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE workflow_evaluations "
                "ADD COLUMN attention_reasons_json TEXT NOT NULL DEFAULT '[]'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                "ALTER TABLE workflow_evaluations "
                "ADD COLUMN follow_up_actions_json TEXT NOT NULL DEFAULT '[]'"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE task_runs ADD COLUMN integrity_hash TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE tool_traces ADD COLUMN integrity_hash TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()

    def _compute_hmac(
        self,
        timestamp: str,
        session_id: str,
        action_type: str,
        tool_name: str,
        input_summary: str,
        output_summary: str,
        status: str,
        tokens: int,
        ms: int,
    ) -> str:
        """Compute HMAC-SHA256 over an audit row's content."""
        message = (
            f"{timestamp}|{session_id}|{action_type}|{tool_name}|"
            f"{input_summary}|{output_summary}|{status}|{tokens}|{ms}"
        )
        return hmac.new(
            self._hmac_key, message.encode(), hashlib.sha256
        ).hexdigest()

    def _compute_workflow_hmac(
        self,
        timestamp: str,
        session_id: str,
        workflow_name: str,
        workflow_identity: str,
        workflow_tags: str,
        user_summary: str,
        status: str,
        failure_reason: str,
        total_tokens: int,
        execution_ms: int,
        llm_calls: int,
        tool_calls: int,
        final_model: str,
        call_chain: str,
    ) -> str:
        """Compute HMAC-SHA256 over a workflow telemetry row."""
        message = (
            f"{timestamp}|{session_id}|{workflow_name}|{workflow_identity}|{workflow_tags}|"
            f"{user_summary}|"
            f"{status}|{failure_reason}|{total_tokens}|{execution_ms}|{llm_calls}|"
            f"{tool_calls}|{final_model}|{call_chain}"
        )
        return hmac.new(
            self._hmac_key, message.encode(), hashlib.sha256
        ).hexdigest()

    def _compute_task_run_hmac(
        self,
        timestamp: str,
        task_id: str,
        session_id: str,
        attempt_number: int,
        worker_id: str,
        status: str,
        failure_reason: str,
        final_output_summary: str,
        execution_ms: int,
    ) -> str:
        """Compute HMAC-SHA256 over a task-run row."""
        message = (
            f"{timestamp}|{task_id}|{session_id}|{attempt_number}|{worker_id}|"
            f"{status}|{failure_reason}|{final_output_summary}|{execution_ms}"
        )
        return hmac.new(
            self._hmac_key, message.encode(), hashlib.sha256
        ).hexdigest()

    def _compute_workflow_evaluation_hmac(
        self,
        timestamp: str,
        workflow_run_id: int,
        session_id: str,
        workflow_name: str,
        workflow_status: str,
        quality_score: int,
        efficiency_score: int,
        feedback_signal: str,
        evaluation_label: str,
        suggestions_json: str,
        failure_classes_json: str,
        attention_reasons_json: str,
        follow_up_actions_json: str,
    ) -> str:
        """Compute HMAC-SHA256 over a workflow-evaluation row."""
        message = (
            f"{timestamp}|{workflow_run_id}|{session_id}|{workflow_name}|"
            f"{workflow_status}|{quality_score}|{efficiency_score}|{feedback_signal}|"
            f"{evaluation_label}|{suggestions_json}|{failure_classes_json}|"
            f"{attention_reasons_json}|{follow_up_actions_json}"
        )
        return hmac.new(
            self._hmac_key, message.encode(), hashlib.sha256
        ).hexdigest()

    def _compute_tool_trace_hmac(
        self,
        timestamp: str,
        task_id: str,
        session_id: str,
        step_id: str,
        attempt_number: int,
        tool_name: str,
        input_summary: str,
        output_summary: str,
        status: str,
        execution_ms: int,
        cached: bool,
    ) -> str:
        """Compute HMAC-SHA256 over a tool-trace row."""
        message = (
            f"{timestamp}|{task_id}|{session_id}|{step_id}|{attempt_number}|"
            f"{tool_name}|{input_summary}|{output_summary}|{status}|{execution_ms}|"
            f"{int(cached)}"
        )
        return hmac.new(
            self._hmac_key, message.encode(), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _sanitize_summary(text: str, limit: int) -> str:
        """Mask obvious secret-shaped tokens before storing one short summary."""
        masked = str(text or "")
        masked = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]+", "Bearer [redacted]", masked)
        masked = re.sub(
            r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*['\"]?[^'\"\s,}]+",
            r"\1=[redacted]",
            masked,
        )
        return masked[:limit]

    def _normalize_audit_row(self, row: sqlite3.Row) -> dict[str, Any]:
        """Return one audit row with replay-safe summaries."""
        item = dict(row)
        item["input_summary"] = self._sanitize_summary(str(item.get("input_summary") or ""), 500)
        item["output_summary"] = self._sanitize_summary(
            str(item.get("output_summary") or ""),
            500,
        )
        return item

    @staticmethod
    def _extract_summary_value(summary: str, key: str) -> str:
        """Extract one `key=value` token from a stored audit summary."""
        prefix = f"{key}="
        for token in str(summary or "").split():
            if token.startswith(prefix):
                return token[len(prefix):]
        return ""

    @staticmethod
    def _clamp_score(value: int) -> int:
        """Clamp one score into the 0-100 range."""
        return max(0, min(100, int(value)))

    @staticmethod
    def _validate_feedback_signal(raw_signal: str) -> str:
        """Validate one workflow feedback signal."""
        signal = str(raw_signal or "unknown").strip().lower() or "unknown"
        allowed = {"unknown", "positive", "neutral", "negative"}
        if signal not in allowed:
            raise ValueError(
                "Feedback signal must be one of: unknown, positive, neutral, negative."
            )
        return signal

    @classmethod
    def _ordered_unique_strings(cls, items: list[str], limit: int = 4) -> list[str]:
        """Return one stable ordered unique list."""
        seen: set[str] = set()
        ordered: list[str] = []
        for item in items:
            normalized = str(item or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
            if len(ordered) >= limit:
                break
        return ordered

    @classmethod
    def _workflow_failure_profile(
        cls,
        *,
        status: str,
        failure_reason: str,
        total_tokens: int,
        execution_ms: int,
        llm_calls: int,
        tool_calls: int,
        call_chain: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build one structured failure profile for a workflow run."""
        normalized_status = str(status or "").strip().lower()
        normalized_reason = str(failure_reason or "").strip().lower()
        failure_classes: list[str] = []
        attention_reasons: list[str] = []
        follow_up_actions: list[str] = []
        quality_penalty = 0
        efficiency_penalty = 0

        tool_error_steps = 0
        llm_error_steps = 0
        timeout_steps = 0
        blocked_steps = 0
        recovery_steps = 0
        for step in call_chain:
            if not isinstance(step, dict):
                continue
            step_type = str(step.get("type") or "").strip().lower()
            step_status = str(step.get("status") or "").strip().lower()
            if step_type == "workflow_role_recovery":
                recovery_steps += 1
            if step_status == "timeout":
                timeout_steps += 1
            elif step_status == "blocked":
                blocked_steps += 1
            elif step_status in {"error", "failed"}:
                if step_type == "llm":
                    llm_error_steps += 1
                else:
                    tool_error_steps += 1

        def _add_issue(
            code: str,
            reason: str,
            action: str,
            *,
            quality: int = 0,
            efficiency: int = 0,
        ) -> None:
            nonlocal quality_penalty, efficiency_penalty
            failure_classes.append(code)
            attention_reasons.append(reason)
            follow_up_actions.append(action)
            quality_penalty += quality
            efficiency_penalty += efficiency

        if normalized_status in {"failed", "error"}:
            _add_issue(
                "workflow_failed",
                "Runs are ending in a failed state.",
                "Audit terminal workflow failures before promoting this path.",
                quality=34,
                efficiency=6,
            )
        elif normalized_status == "degraded":
            _add_issue(
                "workflow_degraded",
                "Runs are leaving the main path in a degraded state.",
                "Review degraded-path routing before expanding this workflow.",
                quality=18,
                efficiency=4,
            )

        if normalized_reason:
            if "timeout" in normalized_reason:
                _add_issue(
                    "provider_timeout",
                    "Timeout-heavy paths are degrading this workflow.",
                    "Switch slow providers or tighten timeout-prone queries.",
                    quality=8,
                    efficiency=16,
                )
            elif normalized_reason in {"evidence_gap", "grounded_search_required"}:
                _add_issue(
                    "evidence_gap",
                    "Grounded evidence coverage is not reaching the final answer.",
                    "Tighten evidence requirements before critic or summarizer handoff.",
                    quality=18,
                    efficiency=6,
                )
            elif normalized_reason.endswith(":error"):
                _add_issue(
                    "tool_failure",
                    "Failing tool steps are destabilizing this workflow.",
                    "Audit failing tool steps and add safer fallback routing.",
                    quality=18,
                    efficiency=8,
                )

        if tool_error_steps and "tool_failure" not in failure_classes:
            _add_issue(
                "tool_failure",
                "Failing tool steps are destabilizing this workflow.",
                "Audit failing tool steps and add safer fallback routing.",
                quality=min(tool_error_steps * 12, 24),
                efficiency=min(tool_error_steps * 6, 12),
            )
        if llm_error_steps:
            _add_issue(
                "llm_failure",
                "Model-side failures are destabilizing this workflow.",
                "Check role prompts, model routing, and JSON-only turn handling.",
                quality=min(llm_error_steps * 14, 28),
                efficiency=min(llm_error_steps * 6, 12),
            )
        if timeout_steps and "provider_timeout" not in failure_classes:
            _add_issue(
                "provider_timeout",
                "Timeout-heavy paths are degrading this workflow.",
                "Switch slow providers or tighten timeout-prone queries.",
                quality=min(timeout_steps * 8, 16),
                efficiency=min(timeout_steps * 12, 24),
            )
        if blocked_steps:
            _add_issue(
                "policy_blocked",
                "Policy blocks are interrupting the workflow path.",
                "Adjust path selection to avoid blocked tools or destinations.",
                quality=min(blocked_steps * 8, 16),
                efficiency=min(blocked_steps * 5, 10),
            )
        if recovery_steps:
            _add_issue(
                "recovery_churn",
                "Recovery is happening often enough to suggest unstable routing.",
                "Inspect recovery loops and simplify the fallback branch policy.",
                quality=min(recovery_steps * 6, 12),
                efficiency=min(recovery_steps * 8, 16),
            )

        if total_tokens > 4000:
            _add_issue(
                "context_pressure",
                "Prompt and evidence volume are too high for this workflow shape.",
                "Trim prompt and evidence volume before the next run.",
                efficiency=24,
            )
        elif total_tokens > 2000:
            _add_issue(
                "context_pressure",
                "Prompt and evidence volume are higher than this workflow needs.",
                "Trim prompt and evidence volume before the next run.",
                efficiency=14,
            )
        elif total_tokens > 1000:
            efficiency_penalty += 6

        if execution_ms > 12000:
            _add_issue(
                "latency_pressure",
                "End-to-end latency is too high for the current workflow path.",
                "Reduce slow steps or parallelize provider work where possible.",
                efficiency=26,
            )
        elif execution_ms > 6000:
            _add_issue(
                "latency_pressure",
                "End-to-end latency is elevated for the current workflow path.",
                "Reduce slow steps or parallelize provider work where possible.",
                efficiency=16,
            )
        elif execution_ms > 3000:
            efficiency_penalty += 8

        if llm_calls >= 4 or tool_calls >= 5:
            _add_issue(
                "iteration_churn",
                "Too many role, tool, or model turns are needed per run.",
                "Collapse unnecessary turns or tool hops in the workflow graph.",
                efficiency=12,
            )
        elif llm_calls >= 3 or tool_calls >= 3:
            efficiency_penalty += 6

        if normalized_reason:
            follow_up_actions.insert(0, "Review failure_reason before expanding this workflow.")

        return {
            "failure_classes": cls._ordered_unique_strings(failure_classes, limit=6),
            "attention_reasons": cls._ordered_unique_strings(attention_reasons, limit=4),
            "follow_up_actions": cls._ordered_unique_strings(follow_up_actions, limit=4),
            "quality_penalty": quality_penalty,
            "efficiency_penalty": efficiency_penalty,
        }

    @classmethod
    def _derive_workflow_recommendation(
        cls,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        """Derive one workflow-level recommendation summary."""
        run_count = max(1, int(item.get("run_count") or 0))
        poor_runs = int(item.get("poor_runs") or 0)
        review_runs = int(item.get("review_runs") or 0)
        non_success_runs = int(item.get("non_success_runs") or 0)
        positive_feedback = int(item.get("positive_feedback") or 0)
        negative_feedback = int(item.get("negative_feedback") or 0)
        avg_quality = int(item.get("avg_quality_score") or 0)
        avg_efficiency = int(item.get("avg_efficiency_score") or 0)
        avg_tokens = int(item.get("avg_tokens") or 0)
        avg_execution_ms = int(item.get("avg_execution_ms") or 0)
        top_failure_class = str(item.get("top_failure_class") or "")
        top_attention_reason = str(item.get("top_attention_reason") or "")
        top_follow_up_action = str(item.get("top_follow_up_action") or "")

        review_rate = review_runs / run_count
        non_success_rate = non_success_runs / run_count
        status = "healthy"
        if (
            poor_runs > 0
            or avg_quality < 65
            or non_success_rate >= 0.25
            or negative_feedback > positive_feedback
            or top_failure_class in {
                "workflow_failed",
                "tool_failure",
                "llm_failure",
                "evidence_gap",
                "provider_timeout",
            }
        ):
            status = "attention"
        elif (
            avg_efficiency < 70
            or review_rate >= 0.5
            or avg_tokens > 1800
            or avg_execution_ms > 5000
            or top_failure_class in {"context_pressure", "latency_pressure", "iteration_churn"}
        ):
            status = "optimize"

        recommendations: list[str] = []
        if run_count < 3:
            recommendations.append(
                "Collect more runs before changing workflow defaults."
            )
        if non_success_runs > 0:
            recommendations.append(
                "Review failure paths and fallback coverage for this workflow."
            )
        if negative_feedback > positive_feedback:
            recommendations.append(
                "Negative feedback outweighs positive feedback; inspect output quality first."
            )
        if avg_quality < 70:
            recommendations.append(
                "Raise evidence quality or tighten tool-selection criteria."
            )
        if avg_efficiency < 70 or avg_tokens > 1800:
            recommendations.append(
                "Reduce prompt or evidence volume to improve efficiency."
            )
        if avg_execution_ms > 5000:
            recommendations.append(
                "Investigate slow providers or extra steps in this workflow."
            )
        if top_attention_reason:
            recommendations.append(top_attention_reason)
        if top_follow_up_action:
            recommendations.append(top_follow_up_action)
        if not recommendations:
            recommendations.append("Keep this workflow as the current baseline.")

        return {
            **item,
            "recommendation_status": status,
            "failure_classes": cls._ordered_unique_strings(
                list(item.get("failure_classes") or []),
                limit=4,
            ),
            "attention_reasons": cls._ordered_unique_strings(
                list(item.get("attention_reasons") or []),
                limit=4,
            ),
            "follow_up_actions": cls._ordered_unique_strings(
                list(item.get("follow_up_actions") or []),
                limit=4,
            ),
            "recommendations": recommendations[:4],
        }

    @classmethod
    def _evaluate_workflow_run(
        cls,
        *,
        status: str,
        failure_reason: str,
        total_tokens: int,
        execution_ms: int,
        llm_calls: int,
        tool_calls: int,
        call_chain: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Derive one structured workflow evaluation from one workflow run."""
        success = str(status).lower() == "success"
        profile = cls._workflow_failure_profile(
            status=status,
            failure_reason=failure_reason,
            total_tokens=total_tokens,
            execution_ms=execution_ms,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            call_chain=call_chain,
        )
        quality_score = cls._clamp_score(92 - int(profile["quality_penalty"]))
        efficiency_score = cls._clamp_score(92 - int(profile["efficiency_penalty"]))

        failure_classes = set(str(item) for item in list(profile.get("failure_classes") or []))
        if success and quality_score >= 82 and efficiency_score >= 76:
            evaluation_label = "good"
        elif (
            quality_score < 55
            or str(status).lower() in {"failed", "error"}
            or (
                str(status).lower() != "success"
                and (
                    efficiency_score < 45
                    or bool({"provider_timeout", "evidence_gap"} & failure_classes)
                )
            )
        ):
            evaluation_label = "poor"
        else:
            evaluation_label = "review"

        follow_up_actions = list(profile.get("follow_up_actions") or [])
        if not follow_up_actions and evaluation_label == "good":
            follow_up_actions.append("Keep current workflow as the baseline path.")

        return {
            "quality_score": quality_score,
            "efficiency_score": efficiency_score,
            "feedback_signal": "unknown",
            "evaluation_label": evaluation_label,
            "suggestions": follow_up_actions[:4],
            "failure_classes": list(profile.get("failure_classes") or [])[:6],
            "attention_reasons": list(profile.get("attention_reasons") or [])[:4],
            "follow_up_actions": follow_up_actions[:4],
        }

    async def log(
        self,
        action_type: str,
        tool_name: Optional[str] = None,
        input_summary: str = "",
        output_summary: str = "",
        status: str = "success",
        tokens: int = 0,
        ms: int = 0,
        session_id: Optional[str] = None,
    ) -> None:
        """
        为每条日志计算integrity_hash。
        Log an action to the audit log.

        Args:
            action_type: Type of action (tool_call, response, blocked, etc.)
            tool_name: Name of tool if applicable
            input_summary: Truncated input (max 500 chars)
            output_summary: Truncated output (max 500 chars)
            status: success, error, blocked, denied, timeout
            tokens: Tokens used
            ms: Execution time in milliseconds
            session_id: Session identifier
        """
        input_summary = self._sanitize_summary(input_summary, 500)
        output_summary = self._sanitize_summary(output_summary, 500)

        def _insert() -> None:
            conn = sqlite3.connect(self.db_path)
            # Get the timestamp that will be used
            cursor = conn.execute("SELECT datetime('now')")
            timestamp = cursor.fetchone()[0]

            integrity = self._compute_hmac(
                timestamp,
                session_id or "",
                action_type,
                tool_name or "",
                input_summary,
                output_summary,
                status,
                tokens,
                ms,
            )

            conn.execute(
                """
                INSERT INTO audit_log
                (timestamp, session_id, action_type, tool_name, input_summary,
                 output_summary, status, tokens_used, execution_ms, integrity_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    session_id,
                    action_type,
                    tool_name,
                    input_summary,
                    output_summary,
                    status,
                    tokens,
                    ms,
                    integrity,
                ),
            )
            conn.commit()
            conn.close()

        await asyncio.to_thread(_insert)

    async def log_workflow_run(
        self,
        session_id: str,
        workflow_name: str,
        workflow_tags: list[str],
        user_summary: str,
        status: str,
        failure_reason: str,
        total_tokens: int,
        execution_ms: int,
        llm_calls: int,
        tool_calls: int,
        final_model: str,
        call_chain: list[dict[str, Any]],
        workflow_identity: str = "",
    ) -> None:
        """Persist one structured workflow summary for a completed agent run."""
        workflow_identity = str(workflow_identity or "").strip()[:80]
        user_summary = self._sanitize_summary(user_summary, 500)
        failure_reason = self._sanitize_summary(failure_reason, 300)
        tags_json = json.dumps(sorted(set(workflow_tags)), ensure_ascii=True)
        chain_json = json.dumps(call_chain, ensure_ascii=True)

        def _insert() -> None:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("SELECT datetime('now')")
            timestamp = cursor.fetchone()[0]
            evaluation = self._evaluate_workflow_run(
                status=status,
                failure_reason=failure_reason,
                total_tokens=total_tokens,
                execution_ms=execution_ms,
                llm_calls=llm_calls,
                tool_calls=tool_calls,
                call_chain=call_chain,
            )

            integrity = self._compute_workflow_hmac(
                timestamp,
                session_id,
                workflow_name,
                workflow_identity,
                tags_json,
                user_summary,
                status,
                failure_reason,
                total_tokens,
                execution_ms,
                llm_calls,
                tool_calls,
                final_model,
                chain_json,
            )

            cursor = conn.execute(
                """
                INSERT INTO workflow_runs
                (timestamp, session_id, workflow_name, workflow_identity, workflow_tags, user_summary,
                 status, failure_reason, total_tokens, execution_ms, llm_calls,
                 tool_calls, final_model, call_chain, integrity_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    session_id,
                    workflow_name,
                    workflow_identity,
                    tags_json,
                    user_summary,
                    status,
                    failure_reason,
                    total_tokens,
                    execution_ms,
                    llm_calls,
                    tool_calls,
                    final_model,
                    chain_json,
                    integrity,
                ),
            )
            workflow_run_id = int(cursor.lastrowid)
            suggestions_json = json.dumps(
                evaluation["suggestions"],
                ensure_ascii=True,
            )
            failure_classes_json = json.dumps(
                evaluation["failure_classes"],
                ensure_ascii=True,
            )
            attention_reasons_json = json.dumps(
                evaluation["attention_reasons"],
                ensure_ascii=True,
            )
            follow_up_actions_json = json.dumps(
                evaluation["follow_up_actions"],
                ensure_ascii=True,
            )
            evaluation_integrity = self._compute_workflow_evaluation_hmac(
                timestamp,
                workflow_run_id,
                session_id,
                workflow_name,
                status,
                int(evaluation["quality_score"]),
                int(evaluation["efficiency_score"]),
                str(evaluation["feedback_signal"]),
                str(evaluation["evaluation_label"]),
                suggestions_json,
                failure_classes_json,
                attention_reasons_json,
                follow_up_actions_json,
            )
            conn.execute(
                """
                INSERT INTO workflow_evaluations
                (timestamp, workflow_run_id, session_id, workflow_name, workflow_status,
                 quality_score, efficiency_score, feedback_signal, evaluation_label,
                 suggestions_json, failure_classes_json, attention_reasons_json,
                 follow_up_actions_json, integrity_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    workflow_run_id,
                    session_id,
                    workflow_name,
                    status,
                    int(evaluation["quality_score"]),
                    int(evaluation["efficiency_score"]),
                    str(evaluation["feedback_signal"]),
                    str(evaluation["evaluation_label"]),
                    suggestions_json,
                    failure_classes_json,
                    attention_reasons_json,
                    follow_up_actions_json,
                    evaluation_integrity,
                ),
            )
            conn.commit()
            conn.close()

        await asyncio.to_thread(_insert)

    async def log_task_run(
        self,
        *,
        task_id: str,
        session_id: str,
        attempt_number: int,
        worker_id: str,
        status: str,
        failure_reason: str = "",
        final_output_summary: str = "",
        execution_ms: int = 0,
    ) -> None:
        """Persist one background task attempt summary."""
        failure_reason = self._sanitize_summary(failure_reason, 300)
        final_output_summary = self._sanitize_summary(final_output_summary, 500)

        def _insert() -> None:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("SELECT datetime('now')")
            timestamp = cursor.fetchone()[0]
            integrity = self._compute_task_run_hmac(
                timestamp,
                task_id,
                session_id,
                int(attempt_number),
                worker_id,
                status,
                failure_reason,
                final_output_summary,
                int(execution_ms),
            )
            conn.execute(
                """
                INSERT INTO task_runs
                (timestamp, task_id, session_id, attempt_number, worker_id, status,
                 failure_reason, final_output_summary, execution_ms, integrity_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    task_id,
                    session_id,
                    int(attempt_number),
                    worker_id,
                    status,
                    failure_reason,
                    final_output_summary,
                    int(execution_ms),
                    integrity,
                ),
            )
            conn.commit()
            conn.close()

        await asyncio.to_thread(_insert)

    async def log_tool_trace(
        self,
        *,
        task_id: str,
        session_id: str,
        step_id: str,
        attempt_number: int,
        tool_name: str,
        input_summary: str = "",
        output_summary: str = "",
        status: str = "success",
        execution_ms: int = 0,
        cached: bool = False,
    ) -> None:
        """Persist one structured tool trace for task replay."""
        input_summary = self._sanitize_summary(input_summary, 500)
        output_summary = self._sanitize_summary(output_summary, 500)

        def _insert() -> None:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("SELECT datetime('now')")
            timestamp = cursor.fetchone()[0]
            integrity = self._compute_tool_trace_hmac(
                timestamp,
                task_id,
                session_id,
                step_id,
                int(attempt_number),
                tool_name,
                input_summary,
                output_summary,
                status,
                int(execution_ms),
                bool(cached),
            )
            conn.execute(
                """
                INSERT INTO tool_traces
                (timestamp, task_id, session_id, step_id, attempt_number, tool_name,
                 input_summary, output_summary, status, execution_ms, cached, integrity_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    task_id,
                    session_id,
                    step_id,
                    int(attempt_number),
                    tool_name,
                    input_summary,
                    output_summary,
                    status,
                    int(execution_ms),
                    1 if cached else 0,
                    integrity,
                ),
            )
            conn.commit()
            conn.close()

        await asyncio.to_thread(_insert)

    async def get_recent(self, limit: int = 50) -> list[dict]:
        """
        Get recent audit log entries.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of log entry dictionaries
        """

        def _query() -> list[dict]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM audit_log
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]

        return await asyncio.to_thread(_query)

    async def get_stats_today(self) -> dict:
        """
        Get today's statistics.

        Returns:
            Dictionary with today's stats
        """

        def _query() -> dict:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as total_actions,
                    COUNT(CASE WHEN action_type = 'response' THEN 1 END) as messages,
                    COUNT(CASE WHEN action_type = 'tool_call' THEN 1 END) as tool_calls,
                    COUNT(CASE WHEN status = 'error' THEN 1 END) as errors,
                    COUNT(CASE WHEN status = 'blocked' THEN 1 END) as blocked,
                    SUM(tokens_used) as total_tokens
                FROM audit_log
                WHERE date(timestamp) = date('now')
                """
            )
            row = cursor.fetchone()
            conn.close()
            return {
                "total_actions": row[0] or 0,
                "messages": row[1] or 0,
                "tool_calls": row[2] or 0,
                "errors": row[3] or 0,
                "blocked": row[4] or 0,
                "total_tokens": row[5] or 0,
            }

        return await asyncio.to_thread(_query) # asyncio.to_thread for non-async DB access

    async def get_boundary_metrics(self, window_hours: int = 24) -> dict[str, Any]:
        """Return aggregated boundary and secret-access metrics for one recent window."""
        window_hours = max(1, min(int(window_hours), 168))

        def _query() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT action_type, tool_name, input_summary, output_summary, status
                FROM audit_log
                WHERE timestamp >= datetime('now', ?)
                  AND action_type IN ('boundary_decision', 'secret_access')
                ORDER BY timestamp DESC, id DESC
                """,
                (f"-{window_hours} hours",),
            ).fetchall()
            conn.close()

            boundary_tool_counts: Counter[str] = Counter()
            secret_tool_counts: Counter[str] = Counter()
            metrics: dict[str, Any] = {
                "window_hours": window_hours,
                "boundary": {
                    "total": 0,
                    "allowed": 0,
                    "blocked": 0,
                    "top_tools": [],
                },
                "secrets": {
                    "total": 0,
                    "granted": 0,
                    "blocked": 0,
                    "missing": 0,
                    "config_sources": 0,
                    "env_sources": 0,
                    "top_tools": [],
                },
            }

            for row in rows:
                row_dict = dict(row)
                action_type = str(row_dict.get("action_type") or "")
                tool_name = str(row_dict.get("tool_name") or "").strip() or "-"
                input_summary = str(row_dict.get("input_summary") or "")
                output_summary = str(row_dict.get("output_summary") or "")
                status = str(row_dict.get("status") or "")

                if action_type == "boundary_decision":
                    metrics["boundary"]["total"] += 1
                    if status == "blocked":
                        metrics["boundary"]["blocked"] += 1
                    else:
                        metrics["boundary"]["allowed"] += 1
                    boundary_tool_counts[tool_name] += 1
                    continue

                if action_type != "secret_access":
                    continue

                metrics["secrets"]["total"] += 1
                decision = self._extract_summary_value(output_summary, "decision")
                source = self._extract_summary_value(input_summary, "source")
                if decision == "blocked":
                    metrics["secrets"]["blocked"] += 1
                elif decision == "missing":
                    metrics["secrets"]["missing"] += 1
                else:
                    metrics["secrets"]["granted"] += 1
                if source.startswith("config:"):
                    metrics["secrets"]["config_sources"] += 1
                elif source.startswith("env:"):
                    metrics["secrets"]["env_sources"] += 1
                secret_tool_counts[tool_name] += 1

            metrics["boundary"]["top_tools"] = [
                {"tool_name": name, "count": count}
                for name, count in boundary_tool_counts.most_common(3)
            ]
            metrics["secrets"]["top_tools"] = [
                {"tool_name": name, "count": count}
                for name, count in secret_tool_counts.most_common(3)
            ]
            return metrics

        return await asyncio.to_thread(_query)

    async def get_recent_workflows(self, limit: int = 10) -> list[dict]:
        """Return recent workflow telemetry entries with parsed JSON fields."""

        def _query() -> list[dict]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM workflow_runs
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = []
            for row in cursor.fetchall():
                rows.append(self._normalize_workflow_row(row))
            conn.close()
            return rows

        return await asyncio.to_thread(_query)

    async def get_recent_workflow_evaluations(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return recent derived workflow evaluations."""

        def _query() -> list[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM workflow_evaluations
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = [self._normalize_workflow_evaluation_row(row) for row in cursor.fetchall()]
            conn.close()
            return rows

        return await asyncio.to_thread(_query)

    async def get_recent_workflow_evaluations_for_session(
        self,
        session_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return recent derived workflow evaluations for one session."""

        def _query() -> list[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM workflow_evaluations
                WHERE session_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (session_id, limit),
            )
            rows = [self._normalize_workflow_evaluation_row(row) for row in cursor.fetchall()]
            conn.close()
            return rows

        return await asyncio.to_thread(_query)

    async def get_workflow_evaluation(
        self,
        workflow_run_id: int,
    ) -> Optional[dict[str, Any]]:
        """Return one derived workflow evaluation by workflow run id."""

        def _query() -> Optional[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM workflow_evaluations
                WHERE workflow_run_id = ?
                LIMIT 1
                """,
                (int(workflow_run_id),),
            ).fetchone()
            conn.close()
            if row is None:
                return None
            return self._normalize_workflow_evaluation_row(row)

        return await asyncio.to_thread(_query)

    async def get_workflow_recommendations(
        self,
        days: int = 7,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return aggregated workflow recommendations for the recent evaluation window."""
        if days < 1 or days > 90:
            raise ValueError("Days must be between 1 and 90.")
        if limit < 1 or limit > 100:
            raise ValueError("Limit must be between 1 and 100.")

        def _query() -> list[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    e.timestamp,
                    e.workflow_run_id,
                    e.workflow_name,
                    e.workflow_status,
                    e.quality_score,
                    e.efficiency_score,
                    e.feedback_signal,
                    e.evaluation_label,
                    e.suggestions_json,
                    e.failure_classes_json,
                    e.attention_reasons_json,
                    e.follow_up_actions_json,
                    w.total_tokens,
                    w.execution_ms,
                    w.status as run_status
                FROM workflow_evaluations e
                JOIN workflow_runs w ON w.id = e.workflow_run_id
                WHERE e.timestamp >= datetime('now', ?)
                ORDER BY e.timestamp DESC, e.id DESC
                """,
                (f"-{int(days)} days",),
            ).fetchall()
            conn.close()

            grouped: dict[str, dict[str, Any]] = {}
            for row in rows:
                item = self._normalize_workflow_evaluation_row(row)
                workflow_name = str(item.get("workflow_name") or "")
                if not workflow_name:
                    continue
                summary = grouped.setdefault(
                    workflow_name,
                    {
                        "workflow_name": workflow_name,
                        "run_count": 0,
                        "good_runs": 0,
                        "review_runs": 0,
                        "poor_runs": 0,
                        "positive_feedback": 0,
                        "neutral_feedback": 0,
                        "negative_feedback": 0,
                        "unknown_feedback": 0,
                        "non_success_runs": 0,
                        "quality_total": 0,
                        "efficiency_total": 0,
                        "token_total": 0,
                        "latency_total": 0,
                        "last_seen_at": "",
                        "failure_class_counts": Counter(),
                        "attention_reason_counts": Counter(),
                        "follow_up_action_counts": Counter(),
                    },
                )
                summary["run_count"] += 1
                label = str(item.get("evaluation_label") or "review")
                if label == "good":
                    summary["good_runs"] += 1
                elif label == "poor":
                    summary["poor_runs"] += 1
                else:
                    summary["review_runs"] += 1
                signal = str(item.get("feedback_signal") or "unknown")
                feedback_key = f"{signal}_feedback"
                if feedback_key in summary:
                    summary[feedback_key] += 1
                else:
                    summary["unknown_feedback"] += 1
                if str(item.get("workflow_status") or "success") != "success":
                    summary["non_success_runs"] += 1
                summary["quality_total"] += int(item.get("quality_score") or 0)
                summary["efficiency_total"] += int(item.get("efficiency_score") or 0)
                summary["token_total"] += int(item.get("total_tokens") or 0)
                summary["latency_total"] += int(item.get("execution_ms") or 0)
                summary["last_seen_at"] = max(
                    summary["last_seen_at"],
                    str(item.get("timestamp") or ""),
                )
                for failure_class in list(item.get("failure_classes") or []):
                    summary["failure_class_counts"][str(failure_class)] += 1
                for reason in list(item.get("attention_reasons") or []):
                    summary["attention_reason_counts"][str(reason)] += 1
                for action in list(item.get("follow_up_actions") or []):
                    summary["follow_up_action_counts"][str(action)] += 1

            recommendations = []
            for summary in grouped.values():
                run_count = max(1, int(summary["run_count"]))
                top_failure_class = ""
                if summary["failure_class_counts"]:
                    top_failure_class = summary["failure_class_counts"].most_common(1)[0][0]
                top_attention_reason = ""
                if summary["attention_reason_counts"]:
                    top_attention_reason = summary["attention_reason_counts"].most_common(1)[0][0]
                top_follow_up_action = ""
                if summary["follow_up_action_counts"]:
                    top_follow_up_action = summary["follow_up_action_counts"].most_common(1)[0][0]
                item = {
                    "workflow_name": summary["workflow_name"],
                    "run_count": summary["run_count"],
                    "good_runs": summary["good_runs"],
                    "review_runs": summary["review_runs"],
                    "poor_runs": summary["poor_runs"],
                    "positive_feedback": summary["positive_feedback"],
                    "neutral_feedback": summary["neutral_feedback"],
                    "negative_feedback": summary["negative_feedback"],
                    "unknown_feedback": summary["unknown_feedback"],
                    "non_success_runs": summary["non_success_runs"],
                    "avg_quality_score": int(summary["quality_total"] / run_count),
                    "avg_efficiency_score": int(summary["efficiency_total"] / run_count),
                    "avg_tokens": int(summary["token_total"] / run_count),
                    "avg_execution_ms": int(summary["latency_total"] / run_count),
                    "last_seen_at": summary["last_seen_at"],
                    "top_failure_class": top_failure_class,
                    "top_attention_reason": top_attention_reason,
                    "top_follow_up_action": top_follow_up_action,
                    "failure_classes": [
                        key for key, _ in summary["failure_class_counts"].most_common(3)
                    ],
                    "attention_reasons": [
                        key for key, _ in summary["attention_reason_counts"].most_common(3)
                    ],
                    "follow_up_actions": [
                        key for key, _ in summary["follow_up_action_counts"].most_common(3)
                    ],
                }
                recommendations.append(self._derive_workflow_recommendation(item))

            severity_rank = {"attention": 0, "optimize": 1, "healthy": 2}
            recommendations.sort(
                key=lambda item: (
                    severity_rank.get(str(item.get("recommendation_status")), 9),
                    int(item.get("avg_quality_score") or 0),
                    int(item.get("avg_efficiency_score") or 0),
                    -int(item.get("run_count") or 0),
                )
            )
            return recommendations[:limit]

        return await asyncio.to_thread(_query)

    async def set_workflow_feedback(
        self,
        workflow_run_id: int,
        feedback_signal: str,
    ) -> dict[str, Any]:
        """Persist one explicit feedback signal for a workflow evaluation."""
        normalized_signal = self._validate_feedback_signal(feedback_signal)

        def _update() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM workflow_evaluations
                WHERE workflow_run_id = ?
                """,
                (int(workflow_run_id),),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Workflow run `{workflow_run_id}` not found.")

            item = dict(row)
            integrity = self._compute_workflow_evaluation_hmac(
                str(item.get("timestamp") or ""),
                int(item.get("workflow_run_id") or 0),
                str(item.get("session_id") or ""),
                str(item.get("workflow_name") or ""),
                str(item.get("workflow_status") or ""),
                int(item.get("quality_score") or 0),
                int(item.get("efficiency_score") or 0),
                normalized_signal,
                str(item.get("evaluation_label") or "review"),
                str(item.get("suggestions_json") or "[]"),
                str(item.get("failure_classes_json") or "[]"),
                str(item.get("attention_reasons_json") or "[]"),
                str(item.get("follow_up_actions_json") or "[]"),
            )
            conn.execute(
                """
                UPDATE workflow_evaluations
                SET feedback_signal = ?, integrity_hash = ?
                WHERE workflow_run_id = ?
                """,
                (normalized_signal, integrity, int(workflow_run_id)),
            )
            conn.commit()
            updated = conn.execute(
                """
                SELECT * FROM workflow_evaluations
                WHERE workflow_run_id = ?
                """,
                (int(workflow_run_id),),
            ).fetchone()
            conn.close()
            return self._normalize_workflow_evaluation_row(updated)

        return await asyncio.to_thread(_update)

    async def get_latest_workflow_evaluation_for_session(
        self,
        session_id: str,
    ) -> Optional[dict[str, Any]]:
        """Return the latest workflow evaluation for one session."""

        def _query() -> Optional[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM workflow_evaluations
                WHERE session_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            conn.close()
            if row is None:
                return None
            return self._normalize_workflow_evaluation_row(row)

        return await asyncio.to_thread(_query)

    async def set_latest_workflow_feedback(
        self,
        session_id: str,
        feedback_signal: str,
    ) -> dict[str, Any]:
        """Update the latest workflow feedback signal for one session."""
        latest = await self.get_latest_workflow_evaluation_for_session(session_id)
        if latest is None:
            raise KeyError(f"No workflow evaluation found for session `{session_id}`.")
        return await self.set_workflow_feedback(
            int(latest["workflow_run_id"]),
            feedback_signal,
        )

    async def get_task_replay(self, task_id: str) -> Optional[dict[str, Any]]:
        """Return one structured replay bundle for a persisted task."""

        def _query() -> Optional[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                conn.close()
                return None

            task = self._normalize_task_row(task_row)
            step_rows = conn.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ?
                ORDER BY created_at ASC, step_id ASC
                """,
                (task_id,),
            ).fetchall()
            run_rows = conn.execute(
                """
                SELECT * FROM task_runs
                WHERE task_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
            tool_rows = conn.execute(
                """
                SELECT * FROM tool_traces
                WHERE task_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
            workflow_rows = conn.execute(
                """
                SELECT * FROM workflow_runs
                WHERE session_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (f"task:{task_id}",),
            ).fetchall()
            audit_rows = conn.execute(
                """
                SELECT * FROM audit_log
                WHERE session_id = ?
                  AND action_type IN (
                      'runtime_watchdog',
                      'runtime_alert',
                      'runtime_alert_escalation',
                      'boundary_decision',
                      'secret_access'
                  )
                ORDER BY timestamp ASC, id ASC
                """,
                (f"task:{task_id}",),
            ).fetchall()
            conn.close()
            return {
                "task": task,
                "steps": [self._normalize_task_step_row(row) for row in step_rows],
                "task_runs": [self._normalize_task_run_row(row) for row in run_rows],
                "tool_traces": [self._normalize_tool_trace_row(row) for row in tool_rows],
                "workflow_runs": [
                    self._normalize_workflow_row(row) for row in workflow_rows
                ],
                "audit_events": [self._normalize_audit_row(row) for row in audit_rows],
            }

        return await asyncio.to_thread(_query)

    async def consume_provider_call(
        self,
        provider_name: str,
        max_calls: int,
    ) -> dict[str, int | bool]:
        """Atomically consume one provider call from a configured quota."""

        def _consume() -> dict[str, int | bool]:
            conn = sqlite3.connect(self.db_path)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO provider_usage (provider_name, used_calls, updated_at)
                VALUES (?, 0, datetime('now'))
                """,
                (provider_name,),
            )
            cursor = conn.execute(
                "SELECT used_calls FROM provider_usage WHERE provider_name = ?",
                (provider_name,),
            )
            row = cursor.fetchone()
            used_calls = int(row[0] or 0) if row else 0

            if max_calls > 0 and used_calls >= max_calls:
                conn.commit()
                conn.close()
                return {
                    "allowed": False,
                    "used_calls": used_calls,
                    "remaining_calls": 0,
                    "max_calls": max_calls,
                }

            used_calls += 1
            conn.execute(
                """
                UPDATE provider_usage
                SET used_calls = ?, updated_at = datetime('now')
                WHERE provider_name = ?
                """,
                (used_calls, provider_name),
            )
            conn.commit()
            conn.close()
            remaining = max_calls - used_calls if max_calls > 0 else -1
            return {
                "allowed": True,
                "used_calls": used_calls,
                "remaining_calls": remaining,
                "max_calls": max_calls,
            }

        return await asyncio.to_thread(_consume)

    async def get_provider_usage(self, provider_name: str, max_calls: int = 0) -> dict[str, int]:
        """Return persisted provider usage summary."""

        def _query() -> dict[str, int]:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                "SELECT used_calls FROM provider_usage WHERE provider_name = ?",
                (provider_name,),
            )
            row = cursor.fetchone()
            conn.close()
            used_calls = int(row[0] or 0) if row else 0
            remaining = max(max_calls - used_calls, 0) if max_calls > 0 else -1
            return {
                "used_calls": used_calls,
                "remaining_calls": remaining,
                "max_calls": max_calls,
            }

        return await asyncio.to_thread(_query)

    async def get_workflow_stats_today(self) -> dict:
        """Return today's aggregate workflow telemetry statistics."""

        def _query() -> dict:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as workflow_runs,
                    COUNT(CASE WHEN status != 'success' THEN 1 END) as failures,
                    SUM(total_tokens) as total_tokens,
                    AVG(execution_ms) as avg_execution_ms,
                    MAX(execution_ms) as max_execution_ms
                FROM workflow_runs
                WHERE date(timestamp) = date('now')
                """
            )
            row = cursor.fetchone()
            conn.close()
            return {
                "workflow_runs": row[0] or 0,
                "failures": row[1] or 0,
                "total_tokens": row[2] or 0,
                "avg_execution_ms": int(row[3] or 0),
                "max_execution_ms": row[4] or 0,
            }

        return await asyncio.to_thread(_query)

    async def get_workflow_evaluation_stats_today(self) -> dict[str, Any]:
        """Return today's aggregate workflow evaluation statistics."""

        def _query() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as evaluations,
                    COUNT(CASE WHEN evaluation_label = 'good' THEN 1 END) as good_runs,
                    COUNT(CASE WHEN evaluation_label = 'review' THEN 1 END) as review_runs,
                    COUNT(CASE WHEN evaluation_label = 'poor' THEN 1 END) as poor_runs,
                    COUNT(CASE WHEN feedback_signal = 'positive' THEN 1 END) as positive_feedback,
                    COUNT(CASE WHEN feedback_signal = 'neutral' THEN 1 END) as neutral_feedback,
                    COUNT(CASE WHEN feedback_signal = 'negative' THEN 1 END) as negative_feedback,
                    COUNT(CASE WHEN feedback_signal = 'unknown' THEN 1 END) as unknown_feedback,
                    AVG(quality_score) as avg_quality_score,
                    AVG(efficiency_score) as avg_efficiency_score
                FROM workflow_evaluations
                WHERE date(timestamp) = date('now')
                """
            )
            row = cursor.fetchone()
            conn.close()
            return {
                "evaluations": row[0] or 0,
                "good_runs": row[1] or 0,
                "review_runs": row[2] or 0,
                "poor_runs": row[3] or 0,
                "positive_feedback": row[4] or 0,
                "neutral_feedback": row[5] or 0,
                "negative_feedback": row[6] or 0,
                "unknown_feedback": row[7] or 0,
                "avg_quality_score": int(row[8] or 0),
                "avg_efficiency_score": int(row[9] or 0),
            }

        return await asyncio.to_thread(_query)

    async def verify_integrity(self) -> tuple[int, int]:
        """
        Verify HMAC integrity of all audit log entries.

        Returns:
            (valid_count, tampered_count) tuple
        """

        def _verify() -> tuple[int, int]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            valid = 0
            tampered = 0
            for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
                row_dict = dict(row)
                stored_hash = row_dict.get("integrity_hash")
                if not stored_hash:
                    # Legacy entry without hash, skip
                    continue
                expected = self._compute_hmac(
                    row_dict["timestamp"],
                    row_dict.get("session_id") or "",
                    row_dict["action_type"],
                    row_dict.get("tool_name") or "",
                    row_dict.get("input_summary") or "",
                    row_dict.get("output_summary") or "",
                    row_dict["status"],
                    row_dict.get("tokens_used") or 0,
                    row_dict.get("execution_ms") or 0,
                )
                if hmac.compare_digest(stored_hash, expected):
                    valid += 1
                else:
                    tampered += 1
                    logger.warning(f"Tampered audit entry id={row_dict['id']}")
            for row in conn.execute("SELECT * FROM workflow_runs ORDER BY id"):
                row_dict = dict(row)
                stored_hash = row_dict.get("integrity_hash")
                if not stored_hash:
                    continue
                expected = self._compute_workflow_hmac(
                    row_dict["timestamp"],
                    row_dict.get("session_id") or "",
                    row_dict.get("workflow_name") or "",
                    row_dict.get("workflow_identity") or "",
                    row_dict.get("workflow_tags") or "[]",
                    row_dict.get("user_summary") or "",
                    row_dict.get("status") or "",
                    row_dict.get("failure_reason") or "",
                    row_dict.get("total_tokens") or 0,
                    row_dict.get("execution_ms") or 0,
                    row_dict.get("llm_calls") or 0,
                    row_dict.get("tool_calls") or 0,
                    row_dict.get("final_model") or "",
                    row_dict.get("call_chain") or "[]",
                )
                if hmac.compare_digest(stored_hash, expected):
                    valid += 1
                else:
                    tampered += 1
                    logger.warning(f"Tampered workflow entry id={row_dict['id']}")
            conn.close()
            return valid, tampered

        return await asyncio.to_thread(_verify)

    async def export_json(self, since: Optional[str] = None) -> str:
        """
        Export audit log as JSON.

        Args:
            since: Optional ISO timestamp to filter from

        Returns:
            JSON string of audit entries
        """
        import json

        def _query() -> str:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            if since:
                cursor = conn.execute(
                    "SELECT * FROM audit_log WHERE timestamp >= ? ORDER BY timestamp",
                    (since,),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM audit_log ORDER BY timestamp"
                )
            rows = cursor.fetchall()
            conn.close()
            return json.dumps([dict(row) for row in rows], indent=2)

        return await asyncio.to_thread(_query)

    @staticmethod
    def _loads_json_list(raw: Any) -> list[Any]:
        """Parse a JSON array field and fall back to an empty list."""
        if isinstance(raw, list):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _loads_json_dict(raw: Any) -> dict[str, Any]:
        """Parse a JSON object field and fall back to an empty dict."""
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _normalize_task_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        """Normalize one persisted task row for replay payloads."""
        item = dict(row)
        item["payload"] = cls._loads_json_dict(item.get("payload"))
        item["cancel_requested"] = bool(item.get("cancel_requested", 0))
        item["dead_lettered"] = bool(item.get("dead_lettered", 0))
        return item

    @classmethod
    def _normalize_task_step_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        """Normalize one persisted task-step row for replay payloads."""
        item = dict(row)
        item["input"] = cls._loads_json_dict(item.pop("input_json", "{}"))
        item["output"] = cls._loads_json_dict(item.pop("output_json", "{}"))
        item["is_checkpoint"] = bool(item.get("is_checkpoint", 0))
        item["idempotent"] = bool(item.get("idempotent", 0))
        return item

    @staticmethod
    def _normalize_task_run_row(row: sqlite3.Row) -> dict[str, Any]:
        """Normalize one persisted task-run row."""
        return dict(row)

    @staticmethod
    def _normalize_tool_trace_row(row: sqlite3.Row) -> dict[str, Any]:
        """Normalize one persisted tool-trace row."""
        item = dict(row)
        item["cached"] = bool(item.get("cached", 0))
        return item

    @classmethod
    def _normalize_workflow_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        """Normalize one workflow telemetry row for replay payloads."""
        item = dict(row)
        item["workflow_tags"] = cls._loads_json_list(item.get("workflow_tags"))
        item["call_chain"] = cls._loads_json_list(item.get("call_chain"))
        item["workflow_identity"] = str(item.get("workflow_identity") or "").strip()
        if not item["workflow_identity"]:
            item["workflow_identity"] = cls._workflow_context_value(
                item,
                name="workflow_identity",
            )
        item["role_checkpoint_timeline"] = [
            {
                "checkpoint_id": str(step.get("checkpoint_id") or ""),
                "role": str(step.get("role") or ""),
                "stage": str(step.get("stage") or ""),
                "message_count": int(step.get("message_count") or 0),
                "evidence_count": int(step.get("evidence_count") or 0),
                "evidence_refs": list(step.get("evidence_refs") or []),
                "evidence_items": list(step.get("evidence_items") or []),
            }
            for step in item["call_chain"]
            if step.get("type") == "workflow_role_checkpoint"
        ]
        item["role_execution_timeline"] = [
            {
                "role": str(step.get("role") or ""),
                "role_label": str(step.get("role_label") or step.get("role") or ""),
                "stage": str(step.get("stage") or ""),
                "checkpoint_id": str(step.get("checkpoint_id") or ""),
                "status": str(step.get("status") or ""),
                "workflow_name": str(step.get("workflow_name") or ""),
                "handler_kind": str(step.get("handler_kind") or ""),
                "brief_content": str(step.get("brief_content") or ""),
                "artifact_preview": str(step.get("artifact_preview") or ""),
                "evidence_refs": list(step.get("evidence_refs") or []),
            }
            for step in item["call_chain"]
            if step.get("type") == "workflow_role_execution"
        ]
        item["role_task_timeline"] = [
            {
                "role": str(step.get("role") or ""),
                "role_label": str(step.get("role_label") or step.get("role") or ""),
                "stage": str(step.get("stage") or ""),
                "task_key": str(step.get("task_key") or ""),
                "status": str(step.get("status") or ""),
                "depends_on": list(step.get("depends_on") or []),
                "checkpoint_id": str(step.get("checkpoint_id") or ""),
                "resume_checkpoint_id": str(step.get("resume_checkpoint_id") or ""),
                "retry_budget": int(step.get("retry_budget") or 0),
                "evidence_refs": list(step.get("evidence_refs") or []),
            }
            for step in item["call_chain"]
            if step.get("type") == "workflow_role_task"
        ]
        item["role_recovery_timeline"] = [
            {
                "failed_role": str(step.get("failed_role") or ""),
                "recovery_role": str(step.get("recovery_role") or ""),
                "stage": str(step.get("stage") or ""),
                "reason": str(step.get("reason") or ""),
                "resume_checkpoint_id": str(step.get("resume_checkpoint_id") or ""),
                "attempt_number": int(step.get("attempt_number") or 0),
                "budget_limit": int(step.get("budget_limit") or 0),
                "remaining_budget": int(step.get("remaining_budget") or 0),
                "restored_messages": int(step.get("restored_messages") or 0),
                "restored_evidence_count": int(step.get("restored_evidence_count") or 0),
                "status": str(step.get("status") or ""),
                "evidence_refs": list(step.get("evidence_refs") or []),
            }
            for step in item["call_chain"]
            if step.get("type") == "workflow_role_recovery"
        ]
        item["role_resume_timeline"] = [
            {
                "role": str(step.get("role") or ""),
                "stage": str(step.get("stage") or ""),
                "resume_checkpoint_id": str(step.get("resume_checkpoint_id") or ""),
                "source_workflow_run_id": int(step.get("source_workflow_run_id") or 0),
                "source_workflow_name": str(step.get("source_workflow_name") or ""),
                "source_status": str(step.get("source_status") or ""),
                "failure_reason": str(step.get("failure_reason") or ""),
                "restored_evidence_count": int(step.get("restored_evidence_count") or 0),
                "status": str(step.get("status") or ""),
                "evidence_refs": list(step.get("evidence_refs") or []),
            }
            for step in item["call_chain"]
            if step.get("type") == "workflow_role_resume"
        ]
        item["role_task_bridge_timeline"] = [
            {
                "task_key": str(step.get("task_key") or ""),
                "role": str(step.get("role") or ""),
                "role_label": str(step.get("role_label") or step.get("role") or ""),
                "stage": str(step.get("stage") or ""),
                "task_type": str(step.get("task_type") or ""),
                "source": str(step.get("source") or ""),
                "description": str(step.get("description") or ""),
                "priority": int(step.get("priority") or 0),
                "timeout_seconds": int(step.get("timeout_seconds") or 0),
                "max_attempts": int(step.get("max_attempts") or 0),
                "idempotency_key": str(step.get("idempotency_key") or ""),
                "payload": dict(step.get("payload") or {}),
                "evidence_refs": list(step.get("evidence_refs") or []),
            }
            for step in item["call_chain"]
            if step.get("type") == "workflow_role_task_bridge"
        ]
        item["shared_evidence_refs"] = sorted(
            {
                str(ref)
                for step in item["call_chain"]
                for ref in list(step.get("evidence_refs") or [])
                if ref
            }
        )
        return item

    @staticmethod
    def _workflow_has_context_value(
        item: dict[str, Any],
        *,
        name: str,
        value: str,
    ) -> bool:
        """Return whether a workflow call chain carries one matching context item."""
        target_name = str(name or "").strip()
        target_value = str(value or "").strip()
        if not target_name or not target_value:
            return False
        for step in list(item.get("call_chain") or []):
            if not isinstance(step, dict):
                continue
            if str(step.get("type") or "").strip() != "workflow_context":
                continue
            if str(step.get("name") or "").strip() != target_name:
                continue
            if str(step.get("value") or "").strip() == target_value:
                return True
        return False

    @staticmethod
    def _workflow_context_value(
        item: dict[str, Any],
        *,
        name: str,
    ) -> str:
        """Return one workflow context value by name when present."""
        target_name = str(name or "").strip()
        if not target_name:
            return ""
        for step in list(item.get("call_chain") or []):
            if not isinstance(step, dict):
                continue
            if str(step.get("type") or "").strip() != "workflow_context":
                continue
            if str(step.get("name") or "").strip() != target_name:
                continue
            return str(step.get("value") or "").strip()
        return ""

    async def get_workflow_role_replay(
        self,
        workflow_run_id: int,
    ) -> Optional[dict[str, Any]]:
        """Return one compact role-level replay view for a workflow run."""

        def _query() -> Optional[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM workflow_runs
                WHERE id = ?
                LIMIT 1
                """,
                (int(workflow_run_id),),
            ).fetchone()
            conn.close()
            if row is None:
                return None
            item = self._normalize_workflow_row(row)
            return {
                "id": item.get("id"),
                "workflow_name": item.get("workflow_name"),
                "workflow_identity": item.get("workflow_identity"),
                "status": item.get("status"),
                "role_checkpoint_timeline": item.get("role_checkpoint_timeline", []),
                "role_execution_timeline": item.get("role_execution_timeline", []),
                "role_task_timeline": item.get("role_task_timeline", []),
                "role_recovery_timeline": item.get("role_recovery_timeline", []),
                "role_resume_timeline": item.get("role_resume_timeline", []),
                "role_task_bridge_timeline": item.get("role_task_bridge_timeline", []),
                "shared_evidence_refs": item.get("shared_evidence_refs", []),
            }

        return await asyncio.to_thread(_query)

    async def get_latest_role_resume_state(
        self,
        session_id: str,
        workflow_name: str,
        workflow_identity: str = "",
        limit: int = 20,
    ) -> Optional[dict[str, Any]]:
        """Return one latest resumable role checkpoint for one session workflow."""

        def _query() -> Optional[dict[str, Any]]:
            def _build_resume_state(item: dict[str, Any]) -> Optional[dict[str, Any]]:
                checkpoints = {
                    str(step.get("checkpoint_id") or ""): step
                    for step in item.get("role_checkpoint_timeline", [])
                    if step.get("checkpoint_id")
                }
                for recovery in reversed(item.get("role_recovery_timeline", [])):
                    checkpoint_id = str(recovery.get("resume_checkpoint_id") or "").strip()
                    checkpoint = checkpoints.get(checkpoint_id)
                    if not checkpoint:
                        continue
                    evidence_items = list(checkpoint.get("evidence_items") or [])
                    evidence_refs = [
                        str(ref)
                        for ref in list(checkpoint.get("evidence_refs") or [])
                        if ref
                    ]
                    return {
                        "source_workflow_run_id": int(item.get("id") or 0),
                        "workflow_name": str(item.get("workflow_name") or ""),
                        "workflow_identity": str(item.get("workflow_identity") or ""),
                        "workflow_status": str(item.get("status") or ""),
                        "failure_reason": str(item.get("failure_reason") or ""),
                        "resume_checkpoint_id": checkpoint_id,
                        "role": str(checkpoint.get("role") or recovery.get("recovery_role") or ""),
                        "stage": str(checkpoint.get("stage") or recovery.get("stage") or ""),
                        "evidence_snapshot": {
                            "count": int(checkpoint.get("evidence_count") or 0),
                            "tools": sorted(
                                {
                                    str(entry.get("tool_name") or "")
                                    for entry in evidence_items
                                    if isinstance(entry, dict) and entry.get("tool_name")
                                }
                            ),
                            "items": evidence_items,
                        },
                        "evidence_refs": evidence_refs,
                    }
                return None

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM workflow_runs
                WHERE workflow_name = ?
                  AND status IN ('degraded', 'stopped', 'error')
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                (workflow_name, max(1, int(limit)) * 5),
            ).fetchall()
            conn.close()
            normalized = [self._normalize_workflow_row(row) for row in rows]
            target_identity = str(workflow_identity or "").strip()
            if target_identity:
                for item in normalized:
                    if str(item.get("workflow_identity") or "").strip() != target_identity:
                        continue
                    resume_state = _build_resume_state(item)
                    if resume_state:
                        return resume_state
            for item in normalized:
                if (
                    str(item.get("session_id") or "").strip() != session_id
                    and not self._workflow_has_context_value(
                        item,
                        name="parent_session_id",
                        value=session_id,
                    )
                ):
                    continue
                resume_state = _build_resume_state(item)
                if resume_state:
                    return resume_state
            return None

        return await asyncio.to_thread(_query)

    async def get_latest_role_task_bridges(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Return the latest role-runtime bridge specs for one session."""

        def _query() -> list[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM workflow_runs
                WHERE session_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            conn.close()
            if row is None:
                return []
            item = self._normalize_workflow_row(row)
            return list(item.get("role_task_bridge_timeline") or [])

        return await asyncio.to_thread(_query)

    @classmethod
    def _normalize_workflow_evaluation_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        """Normalize one workflow evaluation row."""
        item = dict(row)
        item["suggestions"] = cls._loads_json_list(item.pop("suggestions_json", "[]"))
        item["failure_classes"] = cls._loads_json_list(item.pop("failure_classes_json", "[]"))
        item["attention_reasons"] = cls._loads_json_list(item.pop("attention_reasons_json", "[]"))
        item["follow_up_actions"] = cls._loads_json_list(item.pop("follow_up_actions_json", "[]"))
        return item


# Global instance
_audit_log: Optional[AuditLog] = None


def get_audit_log() -> AuditLog:
    """Get the global AuditLog instance."""
    global _audit_log
    if _audit_log is None:
        from nanoclaw.core.config import get_data_path

        _audit_log = AuditLog(get_data_path() / "nanoclaw.db")
    return _audit_log


def set_audit_log(audit: AuditLog) -> None:
    """Set the global AuditLog instance."""
    global _audit_log
    _audit_log = audit
