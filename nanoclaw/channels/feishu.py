"""Feishu channel using event callback webhook."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import aiohttp.web

from nanoclaw.core.llm import ConnectionPool
from nanoclaw.core.logger import get_logger

if TYPE_CHECKING:
    from nanoclaw.channels.gateway import Gateway
    from nanoclaw.core.config import FeishuConfig

logger = get_logger(__name__)


@dataclass
class PendingConfirmation:
    """Pending shell confirmation request waiting for a user reply."""

    user_id: str
    chat_id: str
    future: asyncio.Future[bool]
    created_at: float


@dataclass
class WorkflowReferenceState:
    """Recent workflow list references remembered for one chat."""

    kind: str
    items: list[dict[str, Any]]
    created_at: float


class FeishuChannel:
    """Feishu bot channel backed by an aiohttp webhook server."""

    PAPER_SORT_MODES = {
        "recent",
        "citation",
        "impact",
        "balanced",
        "author",
        "institution",
    }

    PAPER_PROVIDER_ALIASES = {
        "arxiv": "arxiv",
        "openalex": "openalex",
        "semantic_scholar": "semantic_scholar",
        "semanticscholar": "semantic_scholar",
        "s2": "semantic_scholar",
    }
    SCHEDULE_LIST_HEALTH_FILTERS = {"healthy", "retrying", "attention", "muted", "idle"}
    SCHEDULE_LIST_SIGNAL_FILTERS = {"alert", "escalation", "recovery"}
    SCHEDULE_HEALTH_ALIASES = {
        "healthy": "healthy",
        "正常": "healthy",
        "健康": "healthy",
        "retrying": "retrying",
        "重试": "retrying",
        "重试中": "retrying",
        "attention": "attention",
        "关注": "attention",
        "需要关注": "attention",
        "异常": "attention",
        "muted": "muted",
        "静默": "muted",
        "idle": "idle",
        "空闲": "idle",
    }
    SCHEDULE_SIGNAL_ALIASES = {
        "alert": "alert",
        "告警": "alert",
        "escalation": "escalation",
        "升级": "escalation",
        "升级告警": "escalation",
        "recovery": "recovery",
        "恢复": "recovery",
        "已恢复": "recovery",
    }
    FEEDBACK_SIGNAL_ALIASES = {
        "positive": "positive",
        "good": "positive",
        "like": "positive",
        "+": "positive",
        "positive_feedback": "positive",
        "好评": "positive",
        "满意": "positive",
        "赞": "positive",
        "neutral": "neutral",
        "ok": "neutral",
        "meh": "neutral",
        "0": "neutral",
        "一般": "neutral",
        "还行": "neutral",
        "普通": "neutral",
        "negative": "negative",
        "bad": "negative",
        "dislike": "negative",
        "-": "negative",
        "差评": "negative",
        "不满意": "negative",
        "踩": "negative",
    }
    WORKFLOW_RECOMMENDATION_ALIASES = {
        "attention": "attention",
        "关注": "attention",
        "需要关注": "attention",
        "异常": "attention",
        "optimize": "optimize",
        "优化": "optimize",
        "待优化": "optimize",
        "healthy": "healthy",
        "健康": "healthy",
        "正常": "healthy",
    }
    WORKFLOW_FEEDBACK_FILTER_ALIASES = {
        "positive": "positive",
        "正反馈": "positive",
        "好评": "positive",
        "neutral": "neutral",
        "中性反馈": "neutral",
        "一般反馈": "neutral",
        "negative": "negative",
        "负反馈": "negative",
        "差评": "negative",
    }
    WORKFLOW_EVALUATION_LABEL_ALIASES = {
        "good": "good",
        "好": "good",
        "review": "review",
        "review_needed": "review",
        "需复核": "review",
        "复核": "review",
        "poor": "poor",
        "差": "poor",
    }

    SCHEDULE_WEEKDAY_ALIASES = {
        "mon": "mon",
        "monday": "mon",
        "周一": "mon",
        "星期一": "mon",
        "1": "mon",
        "tue": "tue",
        "tuesday": "tue",
        "周二": "tue",
        "星期二": "tue",
        "2": "tue",
        "wed": "wed",
        "wednesday": "wed",
        "周三": "wed",
        "星期三": "wed",
        "3": "wed",
        "thu": "thu",
        "thursday": "thu",
        "周四": "thu",
        "星期四": "thu",
        "4": "thu",
        "fri": "fri",
        "friday": "fri",
        "周五": "fri",
        "星期五": "fri",
        "5": "fri",
        "sat": "sat",
        "saturday": "sat",
        "周六": "sat",
        "星期六": "sat",
        "6": "sat",
        "sun": "sun",
        "sunday": "sun",
        "周日": "sun",
        "星期日": "sun",
        "周天": "sun",
        "星期天": "sun",
        "7": "sun",
        "0": "sun",
    }

    SCHEDULE_WEEKDAY_LABELS = {
        "mon": "Monday",
        "tue": "Tuesday",
        "wed": "Wednesday",
        "thu": "Thursday",
        "fri": "Friday",
        "sat": "Saturday",
        "sun": "Sunday",
    }

    SCHEDULE_WEEKDAY_CRON = {
        "mon": "1",
        "tue": "2",
        "wed": "3",
        "thu": "4",
        "fri": "5",
        "sat": "6",
        "sun": "0",
    }

    def __init__(self, config: "FeishuConfig", gateway: "Gateway"):
        """Initialize Feishu channel state."""
        self.config = config
        self.gateway = gateway
        self.runner: Optional[aiohttp.web.AppRunner] = None
        self._token: str = ""
        self._token_expire_at: float = 0.0
        self._seen_events: dict[str, float] = {}
        self._pending_confirmations: dict[str, PendingConfirmation] = {}
        self._workflow_references: dict[str, WorkflowReferenceState] = {}

    @staticmethod
    def _describe_token(token: str) -> str:
        """Return a safe token description without exposing the token itself."""
        text = str(token or "")
        if not text:
            return "empty"
        return f"len={len(text)}"

    @property
    def enabled(self) -> bool:
        """Return channel enabled state."""
        return self.config.enabled

    async def start(self) -> bool:
        """Start webhook listener. Returns True when listening."""
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu enabled but appId/appSecret is missing")
            return False

        app = aiohttp.web.Application(client_max_size=1024 * 1024)
        app.router.add_post(self.config.webhook_path, self._handle_event)

        self.runner = aiohttp.web.AppRunner(app)
        await self.runner.setup()
        site = aiohttp.web.TCPSite(
            self.runner,
            host=self.config.webhook_host,
            port=self.config.webhook_port,
        )
        await site.start()

        logger.info(
            "Feishu channel started at "
            f"http://{self.config.webhook_host}:{self.config.webhook_port}"
            f"{self.config.webhook_path}"
        )
        return True

    async def stop(self) -> None:
        """Stop webhook listener."""
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

        for pending in self._pending_confirmations.values():
            if not pending.future.done():
                pending.future.set_result(False)
        self._pending_confirmations.clear()

    async def send_proactive(self, text: str) -> None:
        """Send proactive message to configured default chat."""
        if not self.config.default_chat_id:
            logger.warning("Feishu defaultChatId not set, skip proactive message")
            return
        await self._send_text_to_chat(self.config.default_chat_id, text)

    async def send_proactive_to(self, chat_id: str, text: str) -> bool:
        """Send proactive message to a specific Feishu chat id."""
        if not chat_id:
            return False
        return await self._send_text_to_chat(chat_id, text)

    async def _get_tenant_access_token(self) -> Optional[str]:
        """Get or refresh Feishu tenant access token."""
        now = time.time()
        if self._token and now < self._token_expire_at - 30:
            return self._token

        session = await ConnectionPool.get_session()
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.config.app_id,
            "app_secret": self.config.app_secret,
        }

        try:
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"Feishu token request failed: HTTP {resp.status}")
                    return None
                data = await resp.json()
        except Exception as exc:
            logger.error(f"Feishu token request error: {exc}")
            return None

        if data.get("code", 1) != 0:
            logger.error(f"Feishu token request rejected: {data.get('msg', 'unknown')}")
            return None

        token = data.get("tenant_access_token", "")
        expire = int(data.get("expire", 7200))
        if not token:
            return None
        self._token = token
        self._token_expire_at = now + expire
        return token

    async def _send_text_to_chat(self, chat_id: str, text: str) -> bool:
        """Send plain-text message to Feishu chat."""
        token = await self._get_tenant_access_token()
        if not token:
            return False

        session = await ConnectionPool.get_session()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {token}"}
        max_len = 1800
        chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]

        for chunk in chunks:
            payload = {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": chunk}),
            }
            try:
                async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logger.error(f"Feishu send failed: HTTP {resp.status}")
                        return False
                    data = await resp.json()
                if data.get("code", 1) != 0:
                    logger.error(f"Feishu send rejected: {data.get('msg', 'unknown')}")
                    return False
            except Exception as exc:
                logger.error(f"Feishu send error: {exc}")
                return False

        return True

    async def _reply_to_message(self, message_id: str, text: str) -> bool:
        """Reply to Feishu message directly."""
        token = await self._get_tenant_access_token()
        if not token:
            return False

        session = await ConnectionPool.get_session()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
        headers = {"Authorization": f"Bearer {token}"}
        max_len = 1800
        chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [text]

        for chunk in chunks:
            payload = {
                "msg_type": "text",
                "content": json.dumps({"text": chunk}),
            }
            try:
                async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        logger.error(f"Feishu reply failed: HTTP {resp.status}")
                        return False
                    data = await resp.json()
                if data.get("code", 1) != 0:
                    logger.error(f"Feishu reply rejected: {data.get('msg', 'unknown')}")
                    return False
            except Exception as exc:
                logger.error(f"Feishu reply error: {exc}")
                return False

        return True

    def _is_allowed_sender(self, sender: dict[str, Any]) -> bool:
        """Check allow list against open_id/user_id/union_id."""
        if not self.config.allow_from:
            return True

        sender_id = sender.get("sender_id", {})
        candidates = {
            str(sender_id.get("open_id", "")),
            str(sender_id.get("user_id", "")),
            str(sender_id.get("union_id", "")),
        }
        allow = {str(item) for item in self.config.allow_from}
        return any(candidate and candidate in allow for candidate in candidates)

    def _mark_seen_event(self, event_id: str) -> bool:
        """Return False on duplicate event; keep a small in-memory dedupe window."""
        if not event_id:
            return True
        now = time.time()
        if event_id in self._seen_events:
            return False
        self._seen_events[event_id] = now
        if len(self._seen_events) > 512:
            threshold = now - 3600
            self._seen_events = {
                key: ts for key, ts in self._seen_events.items() if ts >= threshold
            }
        return True

    @staticmethod
    def _resolve_sender_id(sender: dict[str, Any]) -> str:
        """Resolve stable sender id from sender payload."""
        sender_id = sender.get("sender_id", {})
        return str(
            sender_id.get("open_id")
            or sender_id.get("user_id")
            or sender_id.get("union_id")
            or "unknown_user"
        )

    @staticmethod
    def _extract_text(raw_content: Any) -> str:
        """Extract plain text from Feishu message content payload."""
        content: dict[str, Any] = {}
        if isinstance(raw_content, str):
            try:
                content = json.loads(raw_content)
            except Exception:
                content = {}
        elif isinstance(raw_content, dict):
            content = raw_content

        text = str(content.get("text_without_at_bot") or content.get("text") or "").strip()
        return text

    @staticmethod
    def _build_session_user_id(chat_id: str, user_id: str) -> str:
        """Build session key body that preserves chat context."""
        if chat_id:
            return f"{chat_id}:{user_id}"
        return user_id

    @classmethod
    def _build_session_id(cls, chat_id: str, user_id: str) -> str:
        """Build the persisted Feishu session id for one chat user."""
        return f"feishu:{cls._build_session_user_id(chat_id, user_id)}"

    @classmethod
    def _paper_usage(cls) -> str:
        """Return usage text for the paper command template."""
        return (
            "Usage:\n"
            "/paper <topic> [--days N] [--max N] [--providers LIST] [--sort MODE]\n"
            "       [--author NAME] [--institution NAME] [--categories CATS]\n\n"
            "Examples:\n"
            "/paper video generation acceleration --days 7 --max 6\n"
            "/paper multimodal agents --providers arxiv,openalex --sort impact\n\n"
            "Notes:\n"
            "- --days range: 1-180\n"
            "- --max range: 3-12\n"
            "- --providers: arxiv, openalex, semantic_scholar, or all\n"
            "- --sort: recent, citation, impact, balanced, author, institution"
        )

    @classmethod
    def _schedule_usage(cls) -> str:
        """Return usage text for Feishu schedule templates."""
        return (
            "Usage:\n"
            "/schedule daily <HH:MM> <topic> [--channels LIST] [--max N] [--days N]\n"
            "                [--workdays] [--weekly WEEKDAY] [--mute START-END]\n"
            "/schedule hotspot <HH:MM> <topic> [--channels LIST] [--max N]\n"
            "                  [--workdays] [--weekly WEEKDAY] [--mute START-END]\n"
            "/schedule paper <HH:MM> <topic> [--days N] [--max N] [--providers LIST]\n"
            "                [--workdays] [--weekly WEEKDAY] [--mute START-END]\n"
            "                [--sort MODE] [--author NAME] [--institution NAME]\n"
            "                [--categories CATS]\n"
            "/schedule update <JOB_ID> <same create syntax>\n"
            "/schedule show <JOB_ID>\n"
            "/schedule pause <JOB_ID>\n"
            "/schedule pause [HEALTH]\n"
            "/schedule pause health <HEALTH> [signal <SIGNAL>]\n"
            "/schedule resume <JOB_ID>\n"
            "/schedule resume [HEALTH]\n"
            "/schedule resume signal <SIGNAL> [health <HEALTH>]\n"
            "/schedule list [HEALTH]\n"
            "/schedule list health <HEALTH> [signal <SIGNAL>]\n"
            "/schedule list signal <SIGNAL> [health <HEALTH>]\n"
            "/schedule remove <JOB_ID>\n\n"
            "/schedule remove [HEALTH]\n"
            "/schedule remove signal <SIGNAL> [health <HEALTH>]\n\n"
            "Examples:\n"
            "/schedule daily 08:30 AI --channels ai,tech --max 6\n"
            "/schedule daily 08:30 AI --workdays\n"
            "/schedule daily 08:30 AI --workdays --mute 22:00-08:00\n"
            "/schedule hotspot 09:00 robotics --weekly fri --max 5\n"
            "/schedule hotspot 21:00 伊朗局势 --channels politics --max 5\n"
            "/schedule paper 22:00 video generation --days 7 --max 6\n"
            "/schedule pause attention\n"
            "/schedule resume signal recovery\n"
            "/schedule list attention\n"
            "/schedule list signal recovery\n\n"
            "/schedule remove muted\n\n"
            "Natural-language shortcuts:\n"
            "每天早上8点给我发一份AI日报\n"
            "工作日早上8点给我发一份AI日报\n"
            "每周一早上9点给我发机器人热点\n"
            "把3号定时任务改成工作日早上8点给我发一份AI日报\n"
            "把AI日报那个定时任务改成工作日早上8点给我发一份AI日报\n"
            "暂停3号定时任务\n"
            "恢复3号定时任务\n"
            "看看3号定时任务\n"
            "暂停AI日报那个定时任务\n"
            "看看机器人热点那个定时任务\n"
            "暂停所有需要关注的定时任务\n"
            "启用所有告警定时任务\n"
            "看看告警的定时任务\n"
            "每天18:00给我推送机器人热点\n"
            "每天9点监控 video generation acceleration 论文 最近7天 最多6篇\n\n"
            "Notes:\n"
            "- Time is interpreted in the local server timezone.\n"
            "- Scheduled Feishu jobs are delivered back to the current chat.\n"
            "- Natural-language scheduling is lightweight and rule-based.\n"
            "- For complex options, prefer the stable `/schedule ...` templates."
        )

    @classmethod
    def _feedback_usage(cls) -> str:
        """Return usage text for chat-scoped workflow feedback."""
        return (
            "Usage:\n"
            "/feedback <positive|neutral|negative>\n\n"
            "Examples:\n"
            "/feedback positive\n"
            "/feedback neutral\n"
            "/feedback negative\n\n"
            "Natural-language shortcuts:\n"
            "这个回答不错\n"
            "这个回复还行\n"
            "这个结果不满意\n\n"
            "给刚才那条工作流好评\n"
            "刚才那条给个差评\n\n"
            "Aliases:\n"
            "- positive: good, like, 好评, 满意\n"
            "- neutral: ok, 一般, 还行\n"
            "- negative: bad, dislike, 差评, 不满意\n\n"
            "Notes:\n"
            "- Feedback applies to the latest workflow run in this Feishu chat session.\n"
            "- Send feedback after the response you want to rate."
        )

    @classmethod
    def _workflow_usage(cls) -> str:
        """Return usage text for workflow recommendation inspection."""
        return (
            "Usage:\n"
            "/workflow report [--days N] [--limit N] [--status STATUS] [--feedback SIGNAL]\n\n"
            "/workflow recent [--limit N] [--label LABEL] [--feedback SIGNAL]\n\n"
            "/workflow feedback <RUN_ID> <SIGNAL>\n\n"
            "/workflow suggest <RUN_ID>\n\n"
            "Examples:\n"
            "/workflow report\n"
            "/workflow report --days 14 --limit 5\n\n"
            "/workflow report --status attention\n"
            "/workflow report --feedback negative\n"
            "/workflow recent --limit 5\n"
            "/workflow recent --label poor\n"
            "/workflow feedback 42 negative\n"
            "/workflow suggest 42\n"
            "Natural-language shortcuts:\n"
            "看看最近工作流建议\n"
            "看看需要关注的工作流建议\n"
            "看看负反馈多的工作流建议\n"
            "看看最近工作流评估\n"
            "看看run42的建议\n"
            "展开刚才那条差评工作流的建议\n"
            "对刚才那条差评工作流给个差评并展开建议\n"
            "把第一条展开\n"
            "给第二条差评\n"
            "把grounded_current_info展开\n"
            "给default_chat_loop差评\n"
            "把grounded_current_info展开并给个差评\n"
            "查看workflow报告\n\n"
            "Notes:\n"
            "- This shows aggregated workflow recommendations, not one chat-only history.\n"
            "- `recent` shows recent workflow evaluations with label and feedback.\n"
            "- `feedback` updates one specific `run#...` from `/workflow recent`.\n"
            "- `suggest` expands the stored suggestions for one specific `run#...`.\n"
            "- Some session-aware shortcuts can update feedback and expand suggestions together.\n"
            "- After `/workflow recent` or `/workflow report`, ordinal shortcuts can reuse the last list.\n"
            "- After `/workflow recent` or `/workflow report`, name shortcuts can reuse the last list too.\n"
            "- `days` range: 1-90\n"
            "- `limit` range: 1-10\n"
            "- `status`: attention, optimize, healthy\n"
            "- `feedback`: positive, neutral, negative\n"
            "- `label`: good, review, poor"
        )

    @classmethod
    def _parse_workflow_status(
        cls,
        raw_value: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Parse one workflow recommendation status alias."""
        normalized = raw_value.strip().lower()
        status = cls.WORKFLOW_RECOMMENDATION_ALIASES.get(normalized)
        if status:
            return status, None
        return None, (
            f"Invalid workflow status `{raw_value}`. "
            "Use attention, optimize, healthy, or their Chinese aliases."
        )

    @classmethod
    def _parse_workflow_feedback_filter(
        cls,
        raw_value: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Parse one workflow-feedback filter alias."""
        normalized = raw_value.strip().lower()
        signal = cls.WORKFLOW_FEEDBACK_FILTER_ALIASES.get(normalized)
        if signal:
            return signal, None
        return None, (
            f"Invalid workflow feedback filter `{raw_value}`. "
            "Use positive, neutral, negative, or their Chinese aliases."
        )

    @classmethod
    def _parse_workflow_evaluation_label(
        cls,
        raw_value: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Parse one workflow-evaluation label alias."""
        normalized = raw_value.strip().lower()
        label = cls.WORKFLOW_EVALUATION_LABEL_ALIASES.get(normalized)
        if label:
            return label, None
        return None, (
            f"Invalid workflow evaluation label `{raw_value}`. "
            "Use good, review, poor, or their Chinese aliases."
        )

    @classmethod
    def _parse_workflow_command(
        cls,
        command_text: str,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Parse one `/workflow` command into recommendation or recent-eval arguments."""
        raw = command_text.strip()
        if not raw.lower().startswith("/workflow"):
            return None, None

        arg_text = raw[len("/workflow"):].strip()
        if not arg_text or arg_text in {"-h", "--help", "help"}:
            return None, cls._workflow_usage()
        try:
            tokens = shlex.split(arg_text)
        except ValueError:
            return None, f"Invalid /workflow command.\n\n{cls._workflow_usage()}"
        if not tokens:
            return None, cls._workflow_usage()
        action = tokens[0].lower()
        if action not in {"report", "recent", "feedback", "suggest"}:
            return None, cls._workflow_usage()
        if action == "suggest":
            if len(tokens) != 2:
                return None, (
                    "Usage:\n/workflow suggest <RUN_ID>\n\n"
                    f"{cls._workflow_usage()}"
                )
            run_id, error = cls._parse_positive_int(
                tokens[1],
                option_name="run_id",
                min_value=1,
                max_value=1_000_000_000,
            )
            if error:
                return None, error
            assert run_id is not None
            return {"action": "suggest", "workflow_run_id": run_id}, None
        if action == "feedback":
            if len(tokens) != 3:
                return None, (
                    "Usage:\n/workflow feedback <RUN_ID> <SIGNAL>\n\n"
                    f"{cls._workflow_usage()}"
                )
            run_id, error = cls._parse_positive_int(
                tokens[1],
                option_name="run_id",
                min_value=1,
                max_value=1_000_000_000,
            )
            if error:
                return None, error
            signal = cls.FEEDBACK_SIGNAL_ALIASES.get(tokens[2].strip().lower())
            if not signal:
                return None, (
                    f"Invalid workflow feedback signal `{tokens[2]}`.\n\n"
                    f"{cls._workflow_usage()}"
                )
            assert run_id is not None
            return {
                "action": "feedback",
                "workflow_run_id": run_id,
                "feedback_signal": signal,
            }, None
        if action == "recent":
            parsed: dict[str, Any] = {
                "action": "recent",
                "limit": 5,
                "label_filter": "",
                "feedback_filter": "",
            }
            index = 1
            while index < len(tokens):
                token = tokens[index]
                key = token
                value = ""
                if token.startswith("--") and "=" in token:
                    key, value = token.split("=", 1)
                elif token in {"--limit", "--max", "--label", "--feedback"}:
                    if index + 1 >= len(tokens):
                        return None, f"Missing value for `{token}`.\n\n{cls._workflow_usage()}"
                    value = tokens[index + 1]
                    index += 1
                elif not parsed["label_filter"]:
                    label, error = cls._parse_workflow_evaluation_label(token)
                    if error:
                        return None, f"Unknown /workflow option `{token}`.\n\n{cls._workflow_usage()}"
                    assert label is not None
                    parsed["label_filter"] = label
                    index += 1
                    continue
                elif not parsed["feedback_filter"]:
                    feedback_signal, error = cls._parse_workflow_feedback_filter(token)
                    if error:
                        return None, f"Unknown /workflow option `{token}`.\n\n{cls._workflow_usage()}"
                    assert feedback_signal is not None
                    parsed["feedback_filter"] = feedback_signal
                    index += 1
                    continue
                else:
                    return None, f"Unknown /workflow option `{token}`.\n\n{cls._workflow_usage()}"

                if key in {"--limit", "--max"}:
                    limit, error = cls._parse_positive_int(
                        value,
                        option_name=key,
                        min_value=1,
                        max_value=10,
                    )
                    if error:
                        return None, error
                    assert limit is not None
                    parsed["limit"] = limit
                elif key == "--label":
                    label, error = cls._parse_workflow_evaluation_label(value)
                    if error:
                        return None, error
                    assert label is not None
                    parsed["label_filter"] = label
                elif key == "--feedback":
                    feedback_signal, error = cls._parse_workflow_feedback_filter(value)
                    if error:
                        return None, error
                    assert feedback_signal is not None
                    parsed["feedback_filter"] = feedback_signal
                index += 1
            return parsed, None

        parsed: dict[str, Any] = {
            "action": "report",
            "days": 7,
            "limit": 5,
            "status_filter": "",
            "feedback_filter": "",
        }
        index = 1
        while index < len(tokens):
            token = tokens[index]
            key = token
            value = ""
            if token.startswith("--") and "=" in token:
                key, value = token.split("=", 1)
            elif token in {"--days", "--limit", "--max", "--status", "--feedback"}:
                if index + 1 >= len(tokens):
                    return None, f"Missing value for `{token}`.\n\n{cls._workflow_usage()}"
                value = tokens[index + 1]
                index += 1
            elif not parsed["status_filter"]:
                status, error = cls._parse_workflow_status(token)
                if error:
                    return None, f"Unknown /workflow option `{token}`.\n\n{cls._workflow_usage()}"
                assert status is not None
                parsed["status_filter"] = status
                index += 1
                continue
            elif not parsed["feedback_filter"]:
                feedback_signal, error = cls._parse_workflow_feedback_filter(token)
                if error:
                    return None, f"Unknown /workflow option `{token}`.\n\n{cls._workflow_usage()}"
                assert feedback_signal is not None
                parsed["feedback_filter"] = feedback_signal
                index += 1
                continue
            else:
                return None, f"Unknown /workflow option `{token}`.\n\n{cls._workflow_usage()}"

            if key == "--days":
                days, error = cls._parse_positive_int(
                    value,
                    option_name="--days",
                    min_value=1,
                    max_value=90,
                )
                if error:
                    return None, error
                assert days is not None
                parsed["days"] = days
            elif key in {"--limit", "--max"}:
                limit, error = cls._parse_positive_int(
                    value,
                    option_name=key,
                    min_value=1,
                    max_value=10,
                )
                if error:
                    return None, error
                assert limit is not None
                parsed["limit"] = limit
            elif key == "--status":
                status, error = cls._parse_workflow_status(value)
                if error:
                    return None, error
                assert status is not None
                parsed["status_filter"] = status
            elif key == "--feedback":
                feedback_signal, error = cls._parse_workflow_feedback_filter(value)
                if error:
                    return None, error
                assert feedback_signal is not None
                parsed["feedback_filter"] = feedback_signal
            index += 1
        return parsed, None

    @classmethod
    def _rewrite_workflow_command_shortcut(
        cls,
        text: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Rewrite one high-confidence workflow-inspection sentence."""
        stripped = text.strip()
        if not stripped or stripped.startswith("/"):
            return None, None
        normalized = re.sub(r"\s+", "", stripped.lower())
        shortcuts = {
            "看看最近工作流建议",
            "查看最近工作流建议",
            "看看工作流建议",
            "查看工作流建议",
            "看看workflow建议",
            "查看workflow建议",
            "查看workflow报告",
            "看看workflow报告",
        }
        if normalized in shortcuts:
            return "/workflow report", None
        recent_shortcuts = {
            "看看最近工作流评估",
            "查看最近工作流评估",
            "看看最近workflow评估",
            "查看最近workflow评估",
            "看看最近工作流表现",
            "查看最近工作流表现",
        }
        if normalized in recent_shortcuts:
            return "/workflow recent", None
        filtered_shortcuts = {
            "看看需要关注的工作流建议": "attention",
            "查看需要关注的工作流建议": "attention",
            "看看attention工作流建议": "attention",
            "查看attention工作流建议": "attention",
            "看看待优化的工作流建议": "optimize",
            "查看待优化的工作流建议": "optimize",
            "看看optimize工作流建议": "optimize",
            "查看optimize工作流建议": "optimize",
            "看看健康的工作流建议": "healthy",
            "查看健康的工作流建议": "healthy",
            "看看healthy工作流建议": "healthy",
            "查看healthy工作流建议": "healthy",
        }
        status = filtered_shortcuts.get(normalized)
        if status:
            return f"/workflow report --status {status}", None
        feedback_shortcuts = {
            "看看负反馈多的工作流建议": "negative",
            "查看负反馈多的工作流建议": "negative",
            "看看negative工作流建议": "negative",
            "查看negative工作流建议": "negative",
            "看看正反馈多的工作流建议": "positive",
            "查看正反馈多的工作流建议": "positive",
            "看看positive工作流建议": "positive",
            "查看positive工作流建议": "positive",
        }
        feedback_signal = feedback_shortcuts.get(normalized)
        if feedback_signal:
            return f"/workflow report --feedback {feedback_signal}", None
        suggest_match = re.match(
            r"^(?:看看|查看|展开)"
            r"(?:run)?#?(\d+)(?:号)?"
            r"(?:的)?(?:workflow|工作流)?建议$",
            normalized,
        )
        if suggest_match:
            return f"/workflow suggest {suggest_match.group(1)}", None
        return None, None

    async def _rewrite_contextual_workflow_shortcut(
        self,
        text: str,
        chat_id: str,
        user_id: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Rewrite one chat-scoped workflow shortcut that needs session context."""
        stripped = text.strip()
        if not stripped or stripped.startswith("/") or not chat_id or not user_id:
            return None, None
        normalized = re.sub(r"\s+", "", stripped.lower())
        label_filter = ""
        feedback_filter = ""
        if normalized in {
            "展开刚才那条工作流的建议",
            "看看刚才那条工作流的建议",
            "查看刚才那条工作流的建议",
        }:
            pass
        elif normalized in {
            "展开刚才那条差评工作流的建议",
            "看看刚才那条差评工作流的建议",
            "查看刚才那条差评工作流的建议",
        }:
            label_filter = "poor"
        elif normalized in {
            "展开刚才那条负反馈工作流的建议",
            "看看刚才那条负反馈工作流的建议",
            "查看刚才那条负反馈工作流的建议",
        }:
            feedback_filter = "negative"
        else:
            return None, None

        from nanoclaw.security.audit import get_audit_log

        session_id = self._build_session_id(chat_id, user_id)
        items = await get_audit_log().get_recent_workflow_evaluations_for_session(
            session_id,
            limit=10,
        )
        if not items:
            return None, (
                "No recent workflow run was found in this chat yet.\n\n"
                "Ask nanoClaw something first, then send `/workflow recent` or "
                "`/workflow suggest <RUN_ID>`."
            )
        items = self._filter_recent_workflow_evaluations(
            items,
            label_filter,
            feedback_filter,
        )
        if not items:
            if label_filter == "poor":
                qualifier = "poor"
            elif feedback_filter == "negative":
                qualifier = "negative-feedback"
            else:
                qualifier = "matching"
            return None, (
                f"No recent {qualifier} workflow run was found in this chat yet.\n\n"
                "Run `/workflow recent` to inspect recent run ids first."
            )
        return f"/workflow suggest {items[0]['workflow_run_id']}", None

    @staticmethod
    def _filter_workflow_recommendations(
        items: list[dict[str, Any]],
        status_filter: str,
        feedback_filter: str,
    ) -> list[dict[str, Any]]:
        """Filter aggregated workflow recommendations by status when requested."""
        filtered = items
        if status_filter:
            filtered = [
                item
                for item in filtered
                if str(item.get("recommendation_status") or "").lower() == status_filter
            ]
        if not feedback_filter:
            return filtered
        feedback_key = f"{feedback_filter}_feedback"
        return [
            item
            for item in filtered
            if int(item.get(feedback_key) or 0) > 0
        ]

    @staticmethod
    def _filter_recent_workflow_evaluations(
        items: list[dict[str, Any]],
        label_filter: str,
        feedback_filter: str,
    ) -> list[dict[str, Any]]:
        """Filter recent workflow evaluations by label or feedback when requested."""
        filtered = items
        if label_filter:
            filtered = [
                item
                for item in filtered
                if str(item.get("evaluation_label") or "").lower() == label_filter
            ]
        if not feedback_filter:
            return filtered
        return [
            item
            for item in filtered
            if str(item.get("feedback_signal") or "").lower() == feedback_filter
        ]

    @staticmethod
    def _format_recent_workflow_evaluation(item: dict[str, Any]) -> list[str]:
        """Format one recent workflow evaluation into Feishu-friendly lines."""
        lines = [
            "- "
            f"run#{item['workflow_run_id']} "
            f"{item['workflow_name']} [{item['evaluation_label']}] "
            f"quality={item['quality_score']} "
            f"efficiency={item['efficiency_score']} "
            f"feedback={item['feedback_signal']}"
        ]
        if item.get("suggestions"):
            lines.append(f"  next={item['suggestions'][0]}")
        return lines

    @staticmethod
    def _format_workflow_suggestion_lines(item: dict[str, Any]) -> list[str]:
        """Format one workflow evaluation into full per-run suggestions."""
        lines = [
            f"Workflow suggestions for run #{item['workflow_run_id']}:",
            (
                f"{item['workflow_name']} [{item['evaluation_label']}] "
                f"status={item['workflow_status']} "
                f"quality={item['quality_score']} "
                f"efficiency={item['efficiency_score']} "
                f"feedback={item['feedback_signal']}"
            ),
        ]
        suggestions = list(item.get("suggestions") or [])
        if suggestions:
            lines.append("Suggestions:")
            lines.extend(f"- {suggestion}" for suggestion in suggestions)
        else:
            lines.append("No follow-up suggestions recorded for this run.")
        return lines

    @staticmethod
    def _format_workflow_recommendation_detail_lines(item: dict[str, Any]) -> list[str]:
        """Format one aggregated workflow recommendation into a detailed view."""
        lines = [
            f"Workflow recommendation for {item['workflow_name']}:",
            (
                f"{item['workflow_name']} [{item['recommendation_status']}] "
                f"runs={item['run_count']} "
                f"quality={item['avg_quality_score']} "
                f"efficiency={item['avg_efficiency_score']}"
            ),
            (
                "feedback="
                f"{item['positive_feedback']}/"
                f"{item['neutral_feedback']}/"
                f"{item['negative_feedback']}"
            ),
        ]
        recommendations = list(item.get("recommendations") or [])
        if recommendations:
            lines.append("Recommendations:")
            lines.extend(f"- {recommendation}" for recommendation in recommendations)
        else:
            lines.append("No follow-up recommendations recorded for this workflow.")
        return lines

    def _prune_workflow_references(self) -> None:
        """Drop stale workflow references and cap in-memory chat state."""
        now = time.time()
        for chat_id in list(self._workflow_references.keys()):
            state = self._workflow_references[chat_id]
            if now - state.created_at > 600:
                del self._workflow_references[chat_id]
        if len(self._workflow_references) <= 256:
            return
        oldest_ids = sorted(
            self._workflow_references.keys(),
            key=lambda item: self._workflow_references[item].created_at,
        )
        for chat_id in oldest_ids[: len(self._workflow_references) - 256]:
            del self._workflow_references[chat_id]

    def _remember_workflow_references(
        self,
        chat_id: str,
        kind: str,
        items: list[dict[str, Any]],
    ) -> None:
        """Remember one recent workflow list for ordinal follow-up actions."""
        if not chat_id or not items:
            return
        self._prune_workflow_references()
        self._workflow_references[chat_id] = WorkflowReferenceState(
            kind=kind,
            items=[dict(item) for item in items],
            created_at=time.time(),
        )

    @staticmethod
    def _normalize_workflow_lookup_text(text: str) -> str:
        """Normalize one workflow reference for cached-list matching."""
        normalized = str(text or "").strip().lower()
        return re.sub(r"[\s`'\"()（）:：;；,，.!！?？]+", "", normalized)

    @classmethod
    def _build_workflow_lookup_labels(cls, item: dict[str, Any]) -> set[str]:
        """Build stable lookup labels for one cached workflow item."""
        workflow_name = str(item.get("workflow_name") or "").strip()
        if not workflow_name:
            return set()
        labels = {
            cls._normalize_workflow_lookup_text(workflow_name),
            cls._normalize_workflow_lookup_text(workflow_name.replace("_", "")),
            cls._normalize_workflow_lookup_text(workflow_name.replace("-", "")),
        }
        return {label for label in labels if label}

    @classmethod
    def _score_named_workflow_match(cls, item: dict[str, Any], reference: str) -> int:
        """Return a simple score for matching one cached workflow item by name."""
        reference_norm = cls._normalize_workflow_lookup_text(reference)
        if len(reference_norm) < 2:
            return 0
        best = 0
        for label in cls._build_workflow_lookup_labels(item):
            if reference_norm == label:
                best = max(best, 400 + len(label))
                continue
            if label.startswith(reference_norm) or label.endswith(reference_norm):
                best = max(best, 320 + len(reference_norm))
                continue
            if reference_norm in label:
                best = max(best, 240 + len(reference_norm))
                continue
            if label in reference_norm:
                best = max(best, 180 + len(label))
        return best

    def _resolve_named_workflow_reference(
        self,
        state: WorkflowReferenceState,
        reference: str,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Resolve one named workflow reference inside a cached workflow list."""
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in state.items:
            score = self._score_named_workflow_match(item, reference)
            if score > 0:
                scored.append((score, item))
        if not scored:
            return None, (
                f"No workflow in the last cached list matched `{reference}`.\n\n"
                "Run `/workflow recent` or `/workflow report` again, then reuse the name or order."
            )
        scored.sort(
            key=lambda pair: (
                -pair[0],
                str(pair[1].get("workflow_name") or ""),
                int(pair[1].get("workflow_run_id") or 0),
            ),
        )
        best_score = scored[0][0]
        top_matches = [item for score, item in scored if score == best_score]
        if len(top_matches) > 1:
            lines = [f"Matched more than one workflow for `{reference}`:"]
            for item in top_matches[:5]:
                lines.append(f"- {item.get('workflow_name')}")
            lines.append("")
            lines.append("Use `把第N条展开`, or mention a more specific workflow name.")
            return None, "\n".join(lines)
        return top_matches[0], None

    @staticmethod
    def _parse_workflow_reference_index(token: str) -> Optional[int]:
        """Parse one 1-based ordinal token used for workflow follow-up actions."""
        mapping = {
            "1": 1,
            "一": 1,
            "2": 2,
            "二": 2,
            "两": 2,
            "3": 3,
            "三": 3,
            "4": 4,
            "四": 4,
            "5": 5,
            "五": 5,
            "6": 6,
            "六": 6,
            "7": 7,
            "七": 7,
            "8": 8,
            "八": 8,
            "9": 9,
            "九": 9,
            "10": 10,
            "十": 10,
        }
        return mapping.get(token)

    async def _handle_workflow_reference_shortcut(
        self,
        text: str,
        chat_id: str,
    ) -> str:
        """Handle one ordinal follow-up action over the last workflow list in a chat."""
        if not chat_id:
            return ""
        normalized = re.sub(r"\s+", "", text.strip().lower())
        if not normalized or normalized.startswith("/"):
            return ""

        expand_match = re.fullmatch(
            r"(?:把|将)?第(10|[1-9]|[一二三四五六七八九十两])条"
            r"(?:工作流|workflow|结果|记录)?(?:展开|查看|看看)(?:一下)?",
            normalized,
        )
        feedback_match = re.fullmatch(
            r"(?:给|对)?第(10|[1-9]|[一二三四五六七八九十两])条"
            r"(?:工作流|workflow|结果|记录)?(?:给个?|来个?)?"
            r"(好评|满意|赞|差评|不满意|踩|还行|一般|普通)",
            normalized,
        )
        combined_match = re.fullmatch(
            r"(?:给|对)?第(10|[1-9]|[一二三四五六七八九十两])条"
            r"(?:工作流|workflow|结果|记录)?(?:给个?|来个?)?"
            r"(好评|满意|赞|差评|不满意|踩|还行|一般|普通)"
            r"(?:并|然后)?(?:展开|查看|看看)(?:一下)?建议?",
            normalized,
        )
        combined_expand_first_match = re.fullmatch(
            r"(?:把|将)?第(10|[1-9]|[一二三四五六七八九十两])条"
            r"(?:工作流|workflow|结果|记录)?(?:展开|查看|看看)(?:一下)?"
            r"(?:并|然后)?(?:给|对)?(?:给个?|来个?)?"
            r"(好评|满意|赞|差评|不满意|踩|还行|一般|普通)",
            normalized,
        )
        action = ""
        index_token = ""
        signal_token = ""
        if combined_match:
            action = "feedback_and_expand"
            index_token = combined_match.group(1)
            signal_token = combined_match.group(2)
        elif combined_expand_first_match:
            action = "feedback_and_expand"
            index_token = combined_expand_first_match.group(1)
            signal_token = combined_expand_first_match.group(2)
        elif feedback_match:
            action = "feedback"
            index_token = feedback_match.group(1)
            signal_token = feedback_match.group(2)
        elif expand_match:
            action = "expand"
            index_token = expand_match.group(1)
        else:
            name_expand_match = re.fullmatch(
                r"(?:把|将)?(.+?)(?:工作流|workflow|结果|记录)?(?:展开|查看|看看)(?:一下)?",
                normalized,
            )
            name_feedback_match = re.fullmatch(
                r"(?:给|对)?(.+?)(?:工作流|workflow|结果|记录)?(?:给个?|来个?)?"
                r"(好评|满意|赞|差评|不满意|踩|还行|一般|普通)",
                normalized,
            )
            name_combined_match = re.fullmatch(
                r"(?:给|对)?(.+?)(?:工作流|workflow|结果|记录)?(?:给个?|来个?)?"
                r"(好评|满意|赞|差评|不满意|踩|还行|一般|普通)"
                r"(?:并|然后)?(?:展开|查看|看看)(?:一下)?建议?",
                normalized,
            )
            name_combined_expand_first_match = re.fullmatch(
                r"(?:把|将)?(.+?)(?:工作流|workflow|结果|记录)?(?:展开|查看|看看)(?:一下)?"
                r"(?:并|然后)?(?:给|对)?(?:给个?|来个?)?"
                r"(好评|满意|赞|差评|不满意|踩|还行|一般|普通)",
                normalized,
            )
            reference = ""
            if name_combined_match:
                action = "feedback_and_expand"
                reference = name_combined_match.group(1)
                signal_token = name_combined_match.group(2)
            elif name_combined_expand_first_match:
                action = "feedback_and_expand"
                reference = name_combined_expand_first_match.group(1)
                signal_token = name_combined_expand_first_match.group(2)
            elif name_feedback_match:
                action = "feedback"
                reference = name_feedback_match.group(1)
                signal_token = name_feedback_match.group(2)
            elif name_expand_match:
                action = "expand"
                reference = name_expand_match.group(1)
            else:
                return ""
            reference = re.sub(r"^(那个|这条|这个)", "", reference).strip()
            if not reference:
                return ""
            index = None

        self._prune_workflow_references()
        state = self._workflow_references.get(chat_id)
        if not state or not state.items:
            return (
                "No recent workflow list is cached in this chat yet.\n\n"
                "Run `/workflow recent` or `/workflow report` first."
            )
        item: Optional[dict[str, Any]]
        if index_token:
            index = self._parse_workflow_reference_index(index_token)
            if index is None:
                return ""
            if index > len(state.items):
                return (
                    f"The last workflow list in this chat only has {len(state.items)} item(s).\n\n"
                    "Run `/workflow recent` or `/workflow report` again if you need a fresh list."
                )
            item = state.items[index - 1]
            item_index = index - 1
        else:
            item, match_error = self._resolve_named_workflow_reference(state, reference)
            if match_error:
                return match_error
            assert item is not None
            item_index = state.items.index(item)
        if action == "expand":
            if state.kind == "recent":
                return "\n".join(self._format_workflow_suggestion_lines(item))
            return "\n".join(self._format_workflow_recommendation_detail_lines(item))

        if state.kind != "recent":
            return (
                "The last workflow list in this chat was an aggregated report.\n\n"
                "Run `/workflow recent` first, or use `/workflow feedback <RUN_ID> <SIGNAL>`."
            )

        signal_map = {
            "好评": "positive",
            "满意": "positive",
            "赞": "positive",
            "差评": "negative",
            "不满意": "negative",
            "踩": "negative",
            "还行": "neutral",
            "一般": "neutral",
            "普通": "neutral",
        }
        signal = signal_map[signal_token]
        from nanoclaw.security.audit import get_audit_log

        updated = await get_audit_log().set_workflow_feedback(int(item["workflow_run_id"]), signal)
        state.items[item_index] = dict(updated)
        self._workflow_references[chat_id] = WorkflowReferenceState(
            kind=state.kind,
            items=state.items,
            created_at=time.time(),
        )
        if action == "feedback":
            return (
                f"Workflow run #{updated['workflow_run_id']} feedback updated to "
                f"{updated['feedback_signal']}."
            )
        lines = [
            f"Workflow run #{updated['workflow_run_id']} feedback updated to "
            f"{updated['feedback_signal']}.",
            "",
        ]
        lines.extend(self._format_workflow_suggestion_lines(updated))
        return "\n".join(lines)

    async def _handle_contextual_workflow_feedback_suggest_shortcut(
        self,
        text: str,
        chat_id: str,
        user_id: str,
    ) -> str:
        """Handle one session-aware workflow feedback-plus-suggest shortcut."""
        if not chat_id or not user_id:
            return ""
        normalized = re.sub(r"\s+", "", text.strip().lower())
        if not normalized or normalized.startswith("/"):
            return ""
        match = re.fullmatch(
            r"(?:对|给)?刚才那条(?:(差评|负反馈))?"
            r"(?:workflow|工作流|回答|回复|结果|输出|分析|内容)?"
            r"(?:给个?|来个?)?"
            r"(好评|满意|赞|差评|不满意|踩|还行|一般|普通)"
            r"(?:并|然后)?(?:展开|查看|看看)(?:一下)?建议",
            normalized,
        )
        if not match:
            return ""

        qualifier = str(match.group(1) or "")
        signal_token = match.group(2)
        signal_map = {
            "好评": "positive",
            "满意": "positive",
            "赞": "positive",
            "差评": "negative",
            "不满意": "negative",
            "踩": "negative",
            "还行": "neutral",
            "一般": "neutral",
            "普通": "neutral",
        }
        signal = signal_map[signal_token]
        label_filter = "poor" if qualifier == "差评" else ""
        feedback_filter = "negative" if qualifier == "负反馈" else ""

        from nanoclaw.security.audit import get_audit_log

        session_id = self._build_session_id(chat_id, user_id)
        audit = get_audit_log()
        items = await audit.get_recent_workflow_evaluations_for_session(session_id, limit=10)
        if not items:
            return (
                "No recent workflow run was found in this chat yet.\n\n"
                "Ask nanoClaw something first, then send `/workflow recent` or "
                "`/feedback positive|neutral|negative`."
            )
        items = self._filter_recent_workflow_evaluations(
            items,
            label_filter,
            feedback_filter,
        )
        if not items:
            if label_filter == "poor":
                qualifier_text = "poor"
            elif feedback_filter == "negative":
                qualifier_text = "negative-feedback"
            else:
                qualifier_text = "matching"
            return (
                f"No recent {qualifier_text} workflow run was found in this chat yet.\n\n"
                "Run `/workflow recent` to inspect recent run ids first."
            )

        updated = await audit.set_workflow_feedback(int(items[0]["workflow_run_id"]), signal)
        lines = [
            f"Workflow run #{updated['workflow_run_id']} feedback updated to "
            f"{updated['feedback_signal']}.",
            "",
        ]
        lines.extend(self._format_workflow_suggestion_lines(updated))
        return "\n".join(lines)

    @classmethod
    def _parse_feedback_command(
        cls,
        command_text: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Parse one `/feedback` command into a normalized signal."""
        raw = command_text.strip()
        if not raw.lower().startswith("/feedback"):
            return None, None

        arg_text = raw[len("/feedback"):].strip()
        if not arg_text or arg_text in {"-h", "--help", "help"}:
            return None, cls._feedback_usage()

        signal = cls.FEEDBACK_SIGNAL_ALIASES.get(arg_text.lower())
        if signal:
            return signal, None
        return None, (
            f"Unknown feedback signal `{arg_text}`.\n\n"
            f"{cls._feedback_usage()}"
        )

    @staticmethod
    def _normalize_feedback_shortcut_text(text: str) -> str:
        """Normalize one natural-language feedback sentence for strict matching."""
        normalized = text.strip().lower()
        normalized = re.sub(r"[\s,，.。!！?？:：;；~～`'\"()（）]+", "", normalized)
        while normalized and normalized[-1] in {"啊", "呀", "呢", "吧", "啦", "哦", "喔", "哈"}:
            normalized = normalized[:-1]
        return normalized

    @classmethod
    def _matches_feedback_phrase(
        cls,
        normalized: str,
        words: tuple[str, ...],
    ) -> bool:
        """Return whether one normalized sentence matches a strict feedback phrase."""
        prefixes = (
            "",
            "这个",
            "这次",
            "上面的",
            "上面这个",
            "刚才的",
            "刚才这个",
            "刚刚的",
            "刚刚这个",
        )
        targets = ("回答", "回复", "结果", "输出", "总结", "分析", "内容")
        for word in words:
            for target in targets:
                if normalized == f"{target}{word}":
                    return True
                for prefix in prefixes[1:]:
                    if normalized == f"{prefix}{target}{word}":
                        return True
        return False

    @classmethod
    def _parse_feedback_shortcut(cls, text: str) -> Optional[str]:
        """Parse a high-confidence natural-language feedback shortcut."""
        raw = text.strip()
        if not raw or raw.startswith("/"):
            return None

        normalized = cls._normalize_feedback_shortcut_text(raw)
        direct_aliases = {
            "好评": "positive",
            "差评": "negative",
        }
        if normalized in direct_aliases:
            return direct_aliases[normalized]
        contextual_aliases = {
            "好评": "positive",
            "满意": "positive",
            "赞": "positive",
            "差评": "negative",
            "不满意": "negative",
            "踩": "negative",
            "还行": "neutral",
            "一般": "neutral",
            "普通": "neutral",
        }
        contextual_match = re.fullmatch(
            r"(?:给)?刚才那条(?:workflow|工作流|回答|回复|结果|输出|分析|内容)?"
            r"(?:给个?|来个?)?(好评|满意|赞|差评|不满意|踩|还行|一般|普通)",
            normalized,
        )
        if contextual_match:
            return contextual_aliases[contextual_match.group(1)]
        contextual_phrase_match = re.fullmatch(
            r"刚才那条(?:workflow|工作流|回答|回复|结果|输出|分析|内容)?"
            r"(不错|挺好|很好|有帮助|还行|一般|普通|不满意|不太行|不行|有问题|不太对|不够好)",
            normalized,
        )
        if contextual_phrase_match:
            token = contextual_phrase_match.group(1)
            if token in {"不错", "挺好", "很好", "有帮助"}:
                return "positive"
            if token in {"还行", "一般", "普通"}:
                return "neutral"
            return "negative"
        if cls._matches_feedback_phrase(normalized, ("不错", "挺好", "很好", "有帮助")):
            return "positive"
        if cls._matches_feedback_phrase(normalized, ("一般", "还行", "普通")):
            return "neutral"
        if cls._matches_feedback_phrase(
            normalized,
            ("不满意", "不太行", "不行", "有问题", "不太对", "不够好"),
        ):
            return "negative"
        return None

    @staticmethod
    def _parse_daily_time(raw_value: str) -> tuple[Optional[str], Optional[str]]:
        """Parse one daily HH:MM time token."""
        value = raw_value.strip()
        match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value)
        if not match:
            return None, f"Invalid time `{raw_value}`. Use HH:MM in 24-hour format."
        hour = int(match.group(1))
        minute = int(match.group(2))
        return f"{hour:02d}:{minute:02d}", None

    @staticmethod
    def _parse_schedule_job_id(raw_value: str) -> tuple[Optional[int], Optional[str]]:
        """Parse one job id used by schedule management commands."""
        try:
            return int(raw_value), None
        except ValueError:
            return None, f"Invalid job id `{raw_value}`."

    @classmethod
    def _parse_schedule_weekday(
        cls,
        raw_value: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Parse one weekday token used by schedule recurrence options."""
        normalized = raw_value.strip().lower()
        weekday = cls.SCHEDULE_WEEKDAY_ALIASES.get(normalized)
        if weekday:
            return weekday, None
        return None, f"Invalid weekday `{raw_value}`. Use mon-sun or 周一-周日."

    @classmethod
    def _parse_quiet_window(
        cls,
        raw_value: str,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Parse one quiet window string like `22:00-08:00`."""
        start_text, sep, end_text = raw_value.partition("-")
        if not sep:
            return None, None, "Invalid --mute window. Use START-END in HH:MM format."
        quiet_start, start_error = cls._parse_daily_time(start_text)
        if start_error:
            return None, None, start_error
        quiet_end, end_error = cls._parse_daily_time(end_text)
        if end_error:
            return None, None, end_error
        assert quiet_start is not None
        assert quiet_end is not None
        return quiet_start, quiet_end, None

    @staticmethod
    def _detect_natural_schedule_kind(text: str) -> str:
        """Detect the workflow kind from a simple natural-language schedule request."""
        lowered = text.lower()
        if "论文" in text or "paper" in lowered or "papers" in lowered:
            return "paper"
        if "热点" in text or "热榜" in text or "hotspot" in lowered:
            return "hotspot"
        if (
            "日报" in text
            or "简报" in text
            or "新闻" in text
            or "digest" in lowered
            or re.search(r"\bnews\b", lowered)
        ):
            return "daily"
        return ""

    @staticmethod
    def _parse_natural_schedule_time(
        text: str,
    ) -> tuple[Optional[str], str, Optional[str]]:
        """Parse a simple Chinese daily time phrase into HH:MM."""
        match = re.search(r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)", text)
        if match:
            hour = int(match.group("hour"))
            minute = int(match.group("minute"))
            return f"{hour:02d}:{minute:02d}", match.group(0), None

        match = re.search(
            r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)?\s*"
            r"(?P<hour>\d{1,2})点(?:(?P<half>半)|(?P<minute>\d{1,2})(?:分)?)?",
            text,
        )
        if not match:
            return None, "", "Could not parse the schedule time."

        hour = int(match.group("hour"))
        minute = 30 if match.group("half") else int(match.group("minute") or 0)
        period = str(match.group("period") or "")
        if minute > 59 or hour > 23:
            return None, "", "Could not parse the schedule time."
        if period in {"下午", "傍晚", "晚上", "晚间"} and 1 <= hour <= 11:
            hour += 12
        elif period == "中午" and 1 <= hour <= 11:
            hour += 12
        elif period in {"凌晨", "早上", "上午"} and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}", match.group(0), None

    @classmethod
    def _detect_natural_schedule_recurrence(
        cls,
        text: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Detect daily/workday/weekly recurrence from simple Chinese text."""
        weekly_match = re.search(r"每周\s*([一二三四五六日天])", text)
        if weekly_match:
            weekday, error = cls._parse_schedule_weekday(f"周{weekly_match.group(1)}")
            if error:
                return None, error
            assert weekday is not None
            return f"--weekly {weekday}", None
        if "工作日" in text:
            return "--workdays", None
        if "每天" in text or "每日" in text:
            return "", None
        return None, None

    @classmethod
    def _strip_schedule_recurrence_tokens(
        cls,
        tokens: list[str],
    ) -> tuple[Optional[list[str]], Optional[dict[str, str]], Optional[str]]:
        """Split recurrence and quiet-window options from one schedule command."""
        remaining: list[str] = []
        recurrence = {
            "schedule_mode": "daily",
            "schedule_weekday": "",
            "quiet_start": "",
            "quiet_end": "",
        }
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if token == "--workdays":
                if recurrence["schedule_mode"] != "daily":
                    return None, None, "Only one of `--workdays` or `--weekly` is allowed."
                recurrence["schedule_mode"] = "workdays"
                idx += 1
                continue

            key = token
            value = ""
            if token.startswith("--weekly="):
                key, value = token.split("=", 1)
            elif token.startswith("--mute="):
                key, value = token.split("=", 1)
            elif token == "--weekly":
                if idx + 1 >= len(tokens):
                    return None, None, "Missing value for option `--weekly`."
                value = tokens[idx + 1]
                idx += 1
            elif token == "--mute":
                if idx + 1 >= len(tokens):
                    return None, None, "Missing value for option `--mute`."
                value = tokens[idx + 1]
                idx += 1

            if key == "--weekly":
                if recurrence["schedule_mode"] != "daily":
                    return None, None, "Only one of `--workdays` or `--weekly` is allowed."
                weekday, error = cls._parse_schedule_weekday(value)
                if error:
                    return None, None, error
                assert weekday is not None
                recurrence["schedule_mode"] = "weekly"
                recurrence["schedule_weekday"] = weekday
                idx += 1
                continue
            if key == "--mute":
                quiet_start, quiet_end, error = cls._parse_quiet_window(value)
                if error:
                    return None, None, error
                assert quiet_start is not None
                assert quiet_end is not None
                recurrence["quiet_start"] = quiet_start
                recurrence["quiet_end"] = quiet_end
                idx += 1
                continue

            remaining.append(token)
            idx += 1

        return remaining, recurrence, None

    @classmethod
    def _rewrite_natural_schedule_command(
        cls,
        text: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Rewrite one simple natural-language daily schedule into `/schedule ...`."""
        stripped = text.strip()
        if not stripped or stripped.startswith("/"):
            return None, None
        recurrence_flag, recurrence_error = cls._detect_natural_schedule_recurrence(stripped)
        if recurrence_error:
            return None, recurrence_error
        if recurrence_flag is None:
            return None, None
        if not any(
            token in stripped
            for token in ("发", "推送", "发送", "监控", "跟踪", "追踪", "订阅", "提醒")
        ):
            return None, None

        kind = cls._detect_natural_schedule_kind(stripped)
        if not kind:
            return None, None

        time_text, time_fragment, time_error = cls._parse_natural_schedule_time(stripped)
        if time_error:
            return None, (
                "Recognized a schedule request, but could not parse the time.\n\n"
                f"{cls._schedule_usage()}"
            )
        assert time_text is not None

        days_match = re.search(r"(?:最近|过去)\s*(\d{1,2})\s*天", stripped)
        max_match = re.search(r"(?:最多|至多|top)\s*(\d{1,2})\s*(?:条|篇|个)?", stripped, re.I)
        channels_match = re.search(
            r"(?:频道|channels?)\s*[:：]?\s*([a-zA-Z0-9_, -]+)",
            stripped,
            re.I,
        )

        topic = stripped.replace(time_fragment, " ", 1)
        cleanup_patterns = [
            r"(每天|每日|工作日|每周\s*[一二三四五六日天])",
            r"(请|麻烦|帮我|以后|之后|从现在开始|开始)",
            r"(给我|发一份|发个|发条|发|推送一份|推送|发送一份|发送)",
            r"(监控|跟踪|追踪|订阅|提醒我|提醒|来一份|来个)",
            r"(?:最近|过去)\s*\d{1,2}\s*天",
            r"(?:最多|至多|top)\s*\d{1,2}\s*(?:条|篇|个)?",
            r"(?:频道|channels?)\s*[:：]?\s*[a-zA-Z0-9_, -]+",
            r"(关于|有关)",
        ]
        if kind == "paper":
            cleanup_patterns.extend([r"(论文监控|论文|papers?|paper monitor)"])
        elif kind == "hotspot":
            cleanup_patterns.extend([r"(热点简报|热点|热榜|hotspot)"])
        else:
            cleanup_patterns.extend([r"(日报|简报|digest|新闻|news)"])

        for pattern in cleanup_patterns:
            topic = re.sub(pattern, " ", topic, flags=re.I)
        topic = re.sub(r"[，,。.!！？:：;；]", " ", topic)
        topic = re.sub(r"\s+", " ", topic).strip()
        if not topic:
            return None, (
                "Recognized a daily schedule request, but the topic was missing.\n\n"
                f"{cls._schedule_usage()}"
            )

        command_parts = ["/schedule", kind, time_text, topic]
        if kind in {"daily", "paper"} and days_match:
            command_parts.extend(["--days", days_match.group(1)])
        if max_match:
            command_parts.extend(["--max", max_match.group(1)])
        if kind in {"daily", "hotspot"} and channels_match:
            command_parts.extend(["--channels", channels_match.group(1).strip()])
        if recurrence_flag:
            command_parts.append(recurrence_flag)
        return " ".join(command_parts), None

    @classmethod
    def _rewrite_schedule_management_shortcut(
        cls,
        text: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Rewrite simple natural-language pause/resume/update schedule commands."""
        stripped = text.strip()
        if not stripped or stripped.startswith("/"):
            return None, None

        match = re.fullmatch(
            r"(?:请|麻烦|帮我)?\s*(暂停|停用|关闭)\s*#?(\d+)\s*号?(?:定时任务|任务|schedule)?",
            stripped,
        )
        if match:
            return f"/schedule pause {match.group(2)}", None

        match = re.fullmatch(
            r"(?:请|麻烦|帮我)?\s*(恢复|启用|重新启用|重新开启)\s*#?(\d+)\s*号?(?:定时任务|任务|schedule)?",
            stripped,
        )
        if match:
            return f"/schedule resume {match.group(2)}", None

        match = re.fullmatch(
            r"(?:请|麻烦|帮我)?(?:看看|查看|显示)\s*#?(\d+)\s*号?(?:定时任务|任务|schedule)(?:详情)?",
            stripped,
        )
        if match:
            return f"/schedule show {match.group(1)}", None

        match = re.fullmatch(
            r"(?:请|麻烦|帮我)?(?:把|将)?\s*#?(\d+)\s*号?(?:定时任务|任务|schedule)"
            r"\s*(?:改成|改为|调整为|换成)\s*(.+)",
            stripped,
        )
        if not match:
            return None, None

        job_id = match.group(1)
        remainder = match.group(2).strip()
        if remainder.lower().startswith(("daily ", "hotspot ", "paper ")):
            return f"/schedule update {job_id} {remainder}", None

        rewritten, error = cls._rewrite_natural_schedule_command(remainder)
        if error:
            return None, error
        if not rewritten:
            return None, (
                "Recognized a schedule update request, but could not parse the new schedule.\n\n"
                f"{cls._schedule_usage()}"
            )
        return f"/schedule update {job_id} {rewritten[len('/schedule '):]}", None

    @staticmethod
    def _normalize_schedule_lookup_text(text: str) -> str:
        """Normalize schedule lookup text for high-confidence name matching."""
        normalized = text.strip().lower()
        replacements = {
            "日报": " 日报 ",
            "热点": " 热点 ",
            "论文": " 论文 ",
            "daily": " daily ",
            "digest": " digest ",
            "hotspot": " hotspot ",
            "paper": " paper ",
        }
        for source, target in replacements.items():
            normalized = normalized.replace(source, target)
        for token in ("那个", "这条", "这个", "定时任务", "任务", "schedule", "feishu"):
            normalized = normalized.replace(token, " ")
        normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    @classmethod
    def _extract_named_schedule_management_request(
        cls,
        text: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Extract a high-confidence `action + reference` request from natural text."""
        stripped = text.strip()
        if not stripped or stripped.startswith("/"):
            return None, None
        patterns = [
            (
                "pause",
                r"(?:请|麻烦|帮我)?\s*(?:暂停|停用|关闭)\s*(.+?)(?:那个|这条|这个)?"
                r"(?:定时任务|任务|schedule)$",
            ),
            (
                "resume",
                r"(?:请|麻烦|帮我)?\s*(?:恢复|启用|重新启用|重新开启)\s*(.+?)"
                r"(?:那个|这条|这个)?(?:定时任务|任务|schedule)$",
            ),
            (
                "show",
                r"(?:请|麻烦|帮我)?(?:看看|查看|显示)\s*(.+?)(?:那个|这条|这个)?"
                r"(?:定时任务|任务|schedule)(?:详情)?$",
            ),
            (
                "remove",
                r"(?:请|麻烦|帮我)?(?:删除|移除|去掉)\s*(.+?)(?:那个|这条|这个)?"
                r"(?:定时任务|任务|schedule)$",
            ),
        ]
        for action, pattern in patterns:
            match = re.fullmatch(pattern, stripped)
            if not match:
                continue
            reference = match.group(1).strip()
            reference = re.sub(r"^(那个|这条|这个)\s*", "", reference).strip()
            if not reference or re.fullmatch(r"#?\d+", reference):
                return None, None
            return action, reference
        return None, None

    @classmethod
    def _extract_named_schedule_update_request(
        cls,
        text: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Extract one high-confidence `reference + new schedule` update request."""
        stripped = text.strip()
        if not stripped or stripped.startswith("/"):
            return None, None
        match = re.fullmatch(
            r"(?:请|麻烦|帮我)?(?:把|将)?\s*(.+?)(?:那个|这条|这个)?"
            r"(?:定时任务|任务|schedule)\s*(?:改成|改为|调整为|换成)\s*(.+)",
            stripped,
        )
        if not match:
            return None, None
        reference = re.sub(r"^(那个|这条|这个)\s*", "", match.group(1).strip()).strip()
        if not reference or re.fullmatch(r"#?\d+", reference):
            return None, None
        remainder = match.group(2).strip()
        if not remainder:
            return None, None
        return reference, remainder

    @classmethod
    def _parse_natural_schedule_filter_value(
        cls,
        raw_value: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Parse one natural-language filter alias into `health` or `signal`."""
        normalized = raw_value.strip().lower()
        for token in ("所有", "全部", "状态", "状态的", "的"):
            normalized = normalized.replace(token, " ")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            return None, None
        health = cls.SCHEDULE_HEALTH_ALIASES.get(normalized)
        if health:
            return "health", health
        signal = cls.SCHEDULE_SIGNAL_ALIASES.get(normalized)
        if signal:
            return "signal", signal
        return None, None

    @classmethod
    def _rewrite_filtered_schedule_management_shortcut(
        cls,
        text: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Rewrite simple natural-language filtered schedule commands."""
        stripped = text.strip()
        if not stripped or stripped.startswith("/"):
            return None, None
        patterns = [
            (
                "pause",
                r"(?:请|麻烦|帮我)?(?:暂停|停用|关闭)\s*(?:所有|全部)?\s*(.+?)\s*"
                r"(?:的)?(?:定时任务|任务|schedule)$",
            ),
            (
                "resume",
                r"(?:请|麻烦|帮我)?(?:恢复|启用|重新启用|重新开启)\s*(?:所有|全部)?\s*(.+?)\s*"
                r"(?:的)?(?:定时任务|任务|schedule)$",
            ),
            (
                "list",
                r"(?:请|麻烦|帮我)?(?:看看|查看|显示|列出)\s*(?:所有|全部)?\s*(.+?)\s*"
                r"(?:的)?(?:定时任务|任务|schedule)(?:列表|详情)?$",
            ),
        ]
        for action, pattern in patterns:
            match = re.fullmatch(pattern, stripped)
            if not match:
                continue
            filter_kind, filter_value = cls._parse_natural_schedule_filter_value(match.group(1))
            if not filter_kind or not filter_value:
                return None, None
            if filter_kind == "health":
                return f"/schedule {action} {filter_value}", None
            return f"/schedule {action} signal {filter_value}", None
        return None, None

    @classmethod
    def _build_schedule_lookup_labels(cls, job: dict[str, Any]) -> set[str]:
        """Build lookup labels from one schedule name and topic."""
        name = str(job.get("name") or "").strip()
        if not name:
            return set()
        topic = name.rsplit(": ", 1)[-1].strip()
        labels = {
            cls._normalize_schedule_lookup_text(name),
            cls._normalize_schedule_lookup_text(topic),
        }
        lowered_name = name.lower()
        if "daily digest" in lowered_name:
            labels.add(cls._normalize_schedule_lookup_text(f"{topic} 日报"))
            labels.add(cls._normalize_schedule_lookup_text(f"{topic} daily digest"))
        elif "hotspot brief" in lowered_name:
            labels.add(cls._normalize_schedule_lookup_text(f"{topic} 热点"))
            labels.add(cls._normalize_schedule_lookup_text(f"{topic} hotspot"))
        elif "paper monitor" in lowered_name:
            labels.add(cls._normalize_schedule_lookup_text(f"{topic} 论文"))
            labels.add(cls._normalize_schedule_lookup_text(f"{topic} paper"))
        return {label for label in labels if label}

    @classmethod
    def _score_named_schedule_match(cls, job: dict[str, Any], reference: str) -> int:
        """Return a simple score for matching one natural-language reference to one job."""
        reference_norm = cls._normalize_schedule_lookup_text(reference)
        if len(reference_norm) < 2:
            return 0
        best = 0
        for label in cls._build_schedule_lookup_labels(job):
            if reference_norm == label:
                best = max(best, 400 + len(label))
                continue
            if label.startswith(reference_norm) or label.endswith(reference_norm):
                best = max(best, 320 + len(reference_norm))
                continue
            if reference_norm in label:
                best = max(best, 240 + len(reference_norm))
                continue
            if label in reference_norm:
                best = max(best, 180 + len(label))
        return best

    async def _resolve_named_schedule_reference(
        self,
        reference: str,
        chat_id: str,
        scheduler: Any,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Resolve one natural-language schedule reference inside the current chat."""
        jobs = await self._list_schedule_jobs(scheduler)
        visible = self._filter_chat_schedule_jobs(jobs, chat_id)
        if not visible:
            return None, (
                "No Feishu schedules exist in this chat yet.\n\n"
                "Run `/schedule list` to see the current `#ID -> task name` mapping."
            )
        scored: list[tuple[int, dict[str, Any]]] = []
        for job in visible:
            score = self._score_named_schedule_match(job, reference)
            if score > 0:
                scored.append((score, job))
        if not scored:
            return None, (
                f"No schedule in this chat matched `{reference}`.\n\n"
                "Run `/schedule list` to see the current `#ID -> task name` mapping."
            )
        scored.sort(key=lambda item: (-item[0], int(item[1]["id"])))
        best_score = scored[0][0]
        top_matches = [job for score, job in scored if score == best_score]
        if len(top_matches) > 1:
            lines = [f"Matched more than one schedule for `{reference}`:"]
            lines.extend(f"- {self._format_schedule_job_ref(job)}" for job in top_matches[:5])
            lines.append("")
            lines.append("Run `/schedule list` and use the `#ID`, or mention a more specific name.")
            return None, "\n".join(lines)
        return top_matches[0], None

    async def _rewrite_named_schedule_management_shortcut(
        self,
        text: str,
        chat_id: str,
        scheduler: Any,
    ) -> tuple[Optional[str], Optional[str]]:
        """Resolve one high-confidence natural-language schedule reference to a job id."""
        reference, remainder = self._extract_named_schedule_update_request(text)
        if reference and remainder:
            matched_job, match_error = await self._resolve_named_schedule_reference(
                reference,
                chat_id,
                scheduler,
            )
            if match_error:
                return None, match_error
            assert matched_job is not None
            if remainder.lower().startswith(("daily ", "hotspot ", "paper ")):
                return f"/schedule update {matched_job['id']} {remainder}", None
            rewritten, error = self._rewrite_natural_schedule_command(remainder)
            if error:
                return None, error
            if not rewritten:
                return None, (
                    "Recognized a schedule update request, but could not parse the new schedule.\n\n"
                    f"{self._schedule_usage()}"
                )
            return (
                f"/schedule update {matched_job['id']} {rewritten[len('/schedule '):]}",
                None,
            )
        action, reference = self._extract_named_schedule_management_request(text)
        if not action or not reference:
            return None, None
        matched_job, match_error = await self._resolve_named_schedule_reference(
            reference,
            chat_id,
            scheduler,
        )
        if match_error:
            return None, match_error
        assert matched_job is not None
        return f"/schedule {action} {matched_job['id']}", None

    @classmethod
    def _build_schedule_cron_expr(cls, arguments: dict[str, Any]) -> str:
        """Convert parsed schedule arguments into a cron expression."""
        time_text = str(arguments.get("time_text") or "")
        hour_text, minute_text = time_text.split(":", 1)
        schedule_mode = str(arguments.get("schedule_mode") or "daily")
        if schedule_mode == "workdays":
            return f"{int(minute_text)} {int(hour_text)} * * 1-5"
        if schedule_mode == "weekly":
            weekday = str(arguments.get("schedule_weekday") or "")
            cron_day = cls.SCHEDULE_WEEKDAY_CRON.get(weekday, "")
            return f"{int(minute_text)} {int(hour_text)} * * {cron_day}"
        return f"{int(minute_text)} {int(hour_text)} * * *"

    @classmethod
    def _describe_schedule_frequency(cls, arguments: dict[str, Any]) -> str:
        """Return a human-readable recurrence label for one schedule."""
        time_text = str(arguments.get("time_text") or "")
        schedule_mode = str(arguments.get("schedule_mode") or "daily")
        if schedule_mode == "workdays":
            return f"every workday at {time_text}"
        if schedule_mode == "weekly":
            weekday = str(arguments.get("schedule_weekday") or "")
            weekday_label = cls.SCHEDULE_WEEKDAY_LABELS.get(weekday, weekday)
            return f"every {weekday_label} at {time_text}"
        return f"every day at {time_text}"

    @staticmethod
    def _describe_quiet_window(source: dict[str, Any]) -> str:
        """Return one human-readable quiet-window label."""
        quiet_start = str(source.get("quiet_start") or "")
        quiet_end = str(source.get("quiet_end") or "")
        if not quiet_start or not quiet_end:
            return "off"
        return f"{quiet_start}-{quiet_end}"

    @staticmethod
    def _describe_schedule_runtime(job: dict[str, Any]) -> list[str]:
        """Return schedule runtime detail lines for one job."""
        runtime = dict(job.get("runtime") or {})
        last_execution = dict(runtime.get("last_execution") or {})
        last_delivery_retry = dict(runtime.get("last_delivery_retry") or {})
        signal_timeline = list(runtime.get("signal_timeline") or [])
        lines = [
            f"Health: {runtime.get('health') or 'idle'}",
            f"Health reason: {runtime.get('health_reason') or 'no recent runtime state'}",
            f"Last execution: {last_execution.get('status') or 'never'}",
            f"Last execution at: {last_execution.get('updated_at') or 'never'}",
            f"Last notify mode: {runtime.get('notify_kind') or 'unknown'}",
            f"Last delivery retry: {last_delivery_retry.get('status') or 'none'}",
        ]
        if last_delivery_retry:
            lines.append(
                f"Last delivery retry at: {last_delivery_retry.get('updated_at') or 'unknown'}"
            )
        if signal_timeline:
            lines.append("Recent schedule signals:")
            for signal in signal_timeline[:3]:
                lines.append(
                    f"- {signal.get('label') or 'signal'}: "
                    f"{signal.get('detail') or '-'} "
                    f"@ {signal.get('timestamp') or 'unknown'}"
                )
        return lines

    def _format_schedule_job_details(self, job: dict[str, Any]) -> str:
        """Format one Feishu schedule into a chat-friendly detail block."""
        enabled = "on" if int(job.get("enabled") or 0) else "off"
        cron_expr = str(job.get("cron_expr") or "")
        lines = [
            f"Feishu schedule #{job['id']}",
            f"Name: {job['name']}",
            f"Enabled: {enabled}",
            f"Cron: {cron_expr or 'interval'}",
            f"Quiet window: {self._describe_quiet_window(job)}",
            f"Created: {job.get('created_at') or 'unknown'}",
            f"Last run: {job.get('last_run') or 'never'}",
        ]
        lines.extend(self._describe_schedule_runtime(job))
        return "\n".join(lines)

    @staticmethod
    def _format_schedule_job_ref(job: dict[str, Any]) -> str:
        """Return one compact `#ID + name` reference for chat replies."""
        return f"#{job['id']} ({job['name']})"

    @classmethod
    def _format_schedule_job_list_entry(cls, job: dict[str, Any]) -> str:
        """Return one chat-friendly schedule list row."""
        cron_expr = str(job.get("cron_expr") or "")
        enabled = "on" if int(job.get("enabled") or 0) else "off"
        health = str(dict(job.get("runtime") or {}).get("health") or "idle")
        return (
            f"- {cls._format_schedule_job_ref(job)} "
            f"[{enabled}] [{health}] {cron_expr or 'interval'}"
        )

    @staticmethod
    async def _list_schedule_jobs(scheduler: Any) -> list[dict[str, Any]]:
        """Load schedule jobs, including runtime state when the scheduler supports it."""
        if hasattr(scheduler, "list_jobs_with_runtime_state"):
            jobs = await scheduler.list_jobs_with_runtime_state()
        else:
            jobs = await scheduler.list_jobs()
        return [dict(job) for job in jobs]

    @classmethod
    def _filter_chat_schedule_jobs(
        cls,
        jobs: list[dict[str, Any]],
        chat_id: str,
        *,
        health_filter: str = "",
        signal_filter: str = "",
    ) -> list[dict[str, Any]]:
        """Return Feishu schedule jobs owned by one chat and matching optional filters."""
        return [
            job
            for job in jobs
            if str(job.get("channel") or "") == "feishu"
            and str(job.get("target_id") or "") == chat_id
            and cls._schedule_job_matches_list_filters(
                job,
                health_filter=health_filter,
                signal_filter=signal_filter,
            )
        ]

    @classmethod
    def _parse_schedule_list_filters(
        cls,
        tokens: list[str],
    ) -> tuple[dict[str, str], Optional[str]]:
        """Parse optional `/schedule list` filters."""
        filters = {"health_filter": "", "signal_filter": ""}
        if not tokens:
            return filters, None
        if len(tokens) == 1:
            token = tokens[0].strip().lower()
            if token in cls.SCHEDULE_LIST_HEALTH_FILTERS:
                filters["health_filter"] = token
                return filters, None
            if token in cls.SCHEDULE_LIST_SIGNAL_FILTERS:
                filters["signal_filter"] = token
                return filters, None
            return {}, (
                "Usage:\n/schedule list [HEALTH]\n"
                "/schedule list health <HEALTH> [signal <SIGNAL>]\n"
                "/schedule list signal <SIGNAL> [health <HEALTH>]"
            )

        index = 0
        while index < len(tokens):
            key = tokens[index].strip().lower()
            if key == "health":
                if index + 1 >= len(tokens):
                    return {}, "Missing value for `health` filter."
                value = tokens[index + 1].strip().lower()
                if value not in cls.SCHEDULE_LIST_HEALTH_FILTERS:
                    return {}, f"Invalid health filter `{tokens[index + 1]}`."
                filters["health_filter"] = value
                index += 2
                continue
            if key == "signal":
                if index + 1 >= len(tokens):
                    return {}, "Missing value for `signal` filter."
                value = tokens[index + 1].strip().lower()
                if value not in cls.SCHEDULE_LIST_SIGNAL_FILTERS:
                    return {}, f"Invalid signal filter `{tokens[index + 1]}`."
                filters["signal_filter"] = value
                index += 2
                continue
            return {}, (
                "Usage:\n/schedule list [HEALTH]\n"
                "/schedule list health <HEALTH> [signal <SIGNAL>]\n"
                "/schedule list signal <SIGNAL> [health <HEALTH>]"
            )
        return filters, None

    @classmethod
    def _schedule_job_matches_list_filters(
        cls,
        job: dict[str, Any],
        *,
        health_filter: str = "",
        signal_filter: str = "",
    ) -> bool:
        """Return whether one schedule job matches `/schedule list` filters."""
        runtime = dict(job.get("runtime") or {})
        health = str(runtime.get("health") or "idle").lower()
        if health_filter and health != health_filter:
            return False
        if not signal_filter:
            return True
        signal_timeline = list(runtime.get("signal_timeline") or [])
        return any(
            str(signal.get("label") or "").lower() == signal_filter
            for signal in signal_timeline
        )

    @staticmethod
    def _describe_schedule_list_filters(
        *,
        health_filter: str = "",
        signal_filter: str = "",
    ) -> str:
        """Return one compact suffix describing active `/schedule list` filters."""
        parts: list[str] = []
        if health_filter:
            parts.append(f"health={health_filter}")
        if signal_filter:
            parts.append(f"signal={signal_filter}")
        return f" ({', '.join(parts)})" if parts else ""

    @staticmethod
    def _parse_positive_int(
        raw_value: str,
        *,
        option_name: str,
        min_value: int,
        max_value: int,
    ) -> tuple[Optional[int], Optional[str]]:
        """Parse and validate one integer option value."""
        try:
            parsed = int(raw_value)
        except ValueError:
            return None, f"Invalid {option_name}: `{raw_value}` is not an integer."
        if parsed < min_value or parsed > max_value:
            return (
                None,
                f"Invalid {option_name}: `{parsed}` out of range ({min_value}-{max_value}).",
            )
        return parsed, None

    @classmethod
    def _parse_paper_command(
        cls,
        command_text: str,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Parse `/paper` command text into paper_search arguments."""
        raw = command_text.strip()
        if not raw.lower().startswith("/paper"):
            return None, None

        arg_text = raw[len("/paper"):].strip()
        if not arg_text or arg_text in {"-h", "--help", "help"}:
            return None, cls._paper_usage()

        try:
            tokens = shlex.split(arg_text)
        except ValueError:
            return None, f"Invalid /paper command: quote parsing failed.\n\n{cls._paper_usage()}"

        parsed: dict[str, Any] = {
            "topic": "",
            "window_days": 14,
            "max_items": 8,
            "providers": "all",
            "sort_by": "recent",
            "author": "",
            "institution": "",
            "categories": "",
        }
        topic_tokens: list[str] = []
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            key = token
            value = ""

            if token.startswith("--") and "=" in token:
                key, value = token.split("=", 1)
            elif token in {
                "--days",
                "-d",
                "--max",
                "-n",
                "--providers",
                "--sort",
                "--author",
                "--institution",
                "--categories",
            }:
                if idx + 1 >= len(tokens):
                    return (
                        None,
                        f"Missing value for option `{token}`.\n\n{cls._paper_usage()}",
                    )
                value = tokens[idx + 1]
                idx += 1
            elif token.startswith("-"):
                return None, f"Unknown /paper option `{token}`.\n\n{cls._paper_usage()}"
            else:
                topic_tokens.append(token)
                idx += 1
                continue

            if key in {"--days", "-d"}:
                parsed_int, err = cls._parse_positive_int(
                    value,
                    option_name="--days",
                    min_value=1,
                    max_value=180,
                )
                if err:
                    return None, err
                parsed["window_days"] = parsed_int
            elif key in {"--max", "-n"}:
                parsed_int, err = cls._parse_positive_int(
                    value,
                    option_name="--max",
                    min_value=3,
                    max_value=12,
                )
                if err:
                    return None, err
                parsed["max_items"] = parsed_int
            elif key == "--providers":
                raw_providers = [item.strip().lower() for item in value.split(",") if item.strip()]
                if not raw_providers:
                    return None, "Invalid --providers: empty value."
                if "all" in raw_providers:
                    parsed["providers"] = "all"
                else:
                    normalized: list[str] = []
                    for item in raw_providers:
                        alias = cls.PAPER_PROVIDER_ALIASES.get(item)
                        if not alias:
                            return (
                                None,
                                "Invalid --providers: supported values are "
                                "arxiv, openalex, semantic_scholar, all.",
                            )
                        if alias not in normalized:
                            normalized.append(alias)
                    parsed["providers"] = ",".join(normalized)
            elif key == "--sort":
                sort_mode = value.strip().lower()
                if sort_mode not in cls.PAPER_SORT_MODES:
                    supported = ", ".join(sorted(cls.PAPER_SORT_MODES))
                    return (
                        None,
                        f"Invalid --sort: `{sort_mode}`. Supported: {supported}.",
                    )
                parsed["sort_by"] = sort_mode
            elif key == "--author":
                parsed["author"] = value.strip()
            elif key == "--institution":
                parsed["institution"] = value.strip()
            elif key == "--categories":
                parsed["categories"] = value.strip()
            idx += 1

        parsed["topic"] = " ".join(topic_tokens).strip()
        if not parsed["topic"]:
            return None, f"Missing topic in /paper command.\n\n{cls._paper_usage()}"
        return parsed, None

    @staticmethod
    def _build_paper_template_prompt(arguments: dict[str, Any]) -> str:
        """Build a strict prompt that encourages one-shot paper_search usage."""
        payload: dict[str, Any] = {
            "topic": arguments["topic"],
            "window_days": arguments["window_days"],
            "max_items": arguments["max_items"],
            "providers": arguments["providers"],
            "sort_by": arguments["sort_by"],
        }
        for optional_key in ("author", "institution", "categories"):
            optional_value = str(arguments.get(optional_key, "")).strip()
            if optional_value:
                payload[optional_key] = optional_value

        payload_text = json.dumps(payload, ensure_ascii=False)
        max_items = int(arguments.get("max_items", 8))
        return (
            "Paper brief request from Feishu template.\n"
            "You must call tool `paper_search` first with exactly these arguments:\n"
            f"{payload_text}\n\n"
            "After tool returns, answer in Chinese with:\n"
            f"1) up to {max_items} papers (title + date + source + URL)\n"
            "2) trend signals and confidence\n"
            "3) a short action recommendation\n"
            "If evidence is insufficient, use the existing rss_miss fallback automatically."
        )

    @staticmethod
    def _build_daily_template_prompt(arguments: dict[str, Any]) -> str:
        """Build a strict prompt for the daily_digest workflow."""
        payload = {
            "topic": arguments["topic"],
            "channels": arguments["channels"],
            "max_items": arguments["max_items"],
            "window_days": arguments["window_days"],
        }
        payload_text = json.dumps(payload, ensure_ascii=False)
        return (
            "Scheduled daily digest request from Feishu template.\n"
            "You must call tool `daily_digest` first with exactly these arguments:\n"
            f"{payload_text}\n\n"
            "After tool returns, answer in Chinese with:\n"
            "1) hot keywords\n"
            f"2) up to {arguments['max_items']} news items (title + date + source + URL)\n"
            "3) a short watchlist recommendation\n"
            "If evidence is insufficient, say so directly."
        )

    @staticmethod
    def _build_hotspot_template_prompt(arguments: dict[str, Any]) -> str:
        """Build a strict prompt for the hotspot_brief workflow."""
        payload = {
            "topic": arguments["topic"],
            "channels": arguments["channels"],
            "max_items": arguments["max_items"],
        }
        payload_text = json.dumps(payload, ensure_ascii=False)
        return (
            "Scheduled hotspot brief request from Feishu template.\n"
            "You must call tool `hotspot_brief` first with exactly these arguments:\n"
            f"{payload_text}\n\n"
            "After tool returns, answer in Chinese with:\n"
            "1) key signals\n"
            f"2) up to {arguments['max_items']} ranked items (title + source + URL)\n"
            "3) one short why-it-matters summary\n"
            "If evidence is insufficient, say so directly."
        )

    @classmethod
    def _parse_schedule_digest_command(
        cls,
        kind: str,
        time_text: str,
        tokens: list[str],
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Parse `/schedule daily|hotspot ...` arguments."""
        parsed: dict[str, Any] = {
            "kind": kind,
            "time_text": time_text,
            "topic": "",
            "channels": "",
            "max_items": 8,
            "window_days": 1,
        }
        topic_tokens: list[str] = []
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            key = token
            value = ""

            if token.startswith("--") and "=" in token:
                key, value = token.split("=", 1)
            elif token in {"--channels", "--max", "--days"}:
                if idx + 1 >= len(tokens):
                    return None, (
                        f"Missing value for option `{token}`.\n\n{cls._schedule_usage()}"
                    )
                value = tokens[idx + 1]
                idx += 1
            elif token.startswith("-"):
                return None, f"Unknown /schedule option `{token}`.\n\n{cls._schedule_usage()}"
            else:
                topic_tokens.append(token)
                idx += 1
                continue

            if key == "--channels":
                parsed["channels"] = value.strip()
            elif key == "--max":
                parsed_int, err = cls._parse_positive_int(
                    value,
                    option_name="--max",
                    min_value=3,
                    max_value=12,
                )
                if err:
                    return None, err
                parsed["max_items"] = parsed_int
            elif key == "--days":
                if kind != "daily":
                    return None, f"`--days` is only supported for `/schedule daily`."
                parsed_int, err = cls._parse_positive_int(
                    value,
                    option_name="--days",
                    min_value=1,
                    max_value=7,
                )
                if err:
                    return None, err
                parsed["window_days"] = parsed_int
            idx += 1

        parsed["topic"] = " ".join(topic_tokens).strip()
        if not parsed["topic"]:
            return None, f"Missing topic in /schedule command.\n\n{cls._schedule_usage()}"
        return parsed, None

    @classmethod
    def _parse_schedule_create_tokens(
        cls,
        tokens: list[str],
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Parse one schedule create/update payload after `/schedule` or `update <id>`."""
        if not tokens:
            return None, cls._schedule_usage()

        action = tokens[0].lower()
        if action not in {"daily", "hotspot", "paper"}:
            return None, cls._schedule_usage()

        stripped_tokens, recurrence, recurrence_error = cls._strip_schedule_recurrence_tokens(
            tokens[1:]
        )
        if recurrence_error:
            return None, f"{recurrence_error}\n\n{cls._schedule_usage()}"
        assert stripped_tokens is not None
        assert recurrence is not None
        if len(stripped_tokens) < 2:
            return None, cls._schedule_usage()

        time_text, time_error = cls._parse_daily_time(stripped_tokens[0])
        if time_error:
            return None, f"{time_error}\n\n{cls._schedule_usage()}"
        assert time_text is not None

        if action == "paper":
            remainder = " ".join(shlex.quote(token) for token in stripped_tokens[1:])
            arguments, parse_error = cls._parse_paper_command(f"/paper {remainder}")
            if parse_error:
                return None, parse_error
            if not arguments:
                return None, cls._paper_usage()
            arguments["kind"] = "paper"
            arguments["time_text"] = time_text
        else:
            arguments, parse_error = cls._parse_schedule_digest_command(
                action,
                time_text,
                stripped_tokens[1:],
            )
            if parse_error:
                return None, parse_error
            if not arguments:
                return None, cls._schedule_usage()

        arguments["schedule_mode"] = recurrence["schedule_mode"]
        arguments["schedule_weekday"] = recurrence["schedule_weekday"]
        arguments["quiet_start"] = recurrence["quiet_start"]
        arguments["quiet_end"] = recurrence["quiet_end"]
        return arguments, None

    @classmethod
    def _parse_schedule_command(
        cls,
        command_text: str,
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Parse `/schedule` command text into one actionable payload."""
        raw = command_text.strip()
        if not raw.lower().startswith("/schedule"):
            return None, None

        arg_text = raw[len("/schedule"):].strip()
        if not arg_text or arg_text in {"-h", "--help", "help"}:
            return None, cls._schedule_usage()

        try:
            tokens = shlex.split(arg_text)
        except ValueError:
            return None, (
                "Invalid /schedule command: quote parsing failed.\n\n"
                f"{cls._schedule_usage()}"
            )
        if not tokens:
            return None, cls._schedule_usage()

        action = tokens[0].lower()
        if action == "list":
            filters, filter_error = cls._parse_schedule_list_filters(tokens[1:])
            if filter_error:
                return None, filter_error
            return {"action": "list", **filters}, None
        if action == "remove":
            if len(tokens) == 2:
                job_id, error = cls._parse_schedule_job_id(tokens[1])
                if not error:
                    assert job_id is not None
                    return {"action": "remove", "job_id": job_id}, None
            filters, filter_error = cls._parse_schedule_list_filters(tokens[1:])
            if filter_error:
                return None, (
                    "Usage:\n/schedule remove <JOB_ID>\n"
                    "/schedule remove [HEALTH]\n"
                    "/schedule remove signal <SIGNAL> [health <HEALTH>]\n\n"
                    f"{cls._schedule_usage()}"
                )
            if not filters.get("health_filter") and not filters.get("signal_filter"):
                return None, (
                    "Usage:\n/schedule remove <JOB_ID>\n"
                    "/schedule remove [HEALTH]\n"
                    "/schedule remove signal <SIGNAL> [health <HEALTH>]\n\n"
                    f"{cls._schedule_usage()}"
                )
            return {"action": "remove_matching", **filters}, None
        if action == "show":
            if len(tokens) != 2:
                return None, f"Usage:\n/schedule show <JOB_ID>\n\n{cls._schedule_usage()}"
            job_id, error = cls._parse_schedule_job_id(tokens[1])
            if error:
                return None, error
            assert job_id is not None
            return {"action": "show", "job_id": job_id}, None
        if action in {"pause", "resume"}:
            if len(tokens) == 2:
                job_id, error = cls._parse_schedule_job_id(tokens[1])
                if not error:
                    assert job_id is not None
                    return {"action": action, "job_id": job_id}, None
            filters, filter_error = cls._parse_schedule_list_filters(tokens[1:])
            if filter_error:
                return None, (
                    f"Usage:\n/schedule {action} <JOB_ID>\n"
                    f"/schedule {action} [HEALTH]\n"
                    f"/schedule {action} health <HEALTH> [signal <SIGNAL>]\n\n"
                    f"{cls._schedule_usage()}"
                )
            if not filters.get("health_filter") and not filters.get("signal_filter"):
                return None, (
                    f"Usage:\n/schedule {action} <JOB_ID>\n"
                    f"/schedule {action} [HEALTH]\n"
                    f"/schedule {action} health <HEALTH> [signal <SIGNAL>]\n\n"
                    f"{cls._schedule_usage()}"
                )
            return {"action": f"{action}_matching", **filters}, None
        if action == "update":
            if len(tokens) < 4:
                return None, f"Usage:\n/schedule update <JOB_ID> <schedule>\n\n{cls._schedule_usage()}"
            job_id, error = cls._parse_schedule_job_id(tokens[1])
            if error:
                return None, error
            assert job_id is not None
            arguments, parse_error = cls._parse_schedule_create_tokens(tokens[2:])
            if parse_error:
                return None, parse_error
            if not arguments:
                return None, cls._schedule_usage()
            return {"action": "update", "job_id": job_id, "arguments": arguments}, None

        arguments, parse_error = cls._parse_schedule_create_tokens(tokens)
        if parse_error:
            return None, parse_error
        if not arguments:
            return None, cls._schedule_usage()
        return {"action": "create", "arguments": arguments}, None

    def _build_schedule_job_payload(
        self,
        arguments: dict[str, Any],
    ) -> tuple[str, str, str, str, str]:
        """Build cron job payload and summary labels for one Feishu schedule."""
        kind = str(arguments.get("kind") or "")
        schedule_text = self._describe_schedule_frequency(arguments)
        quiet_text = self._describe_quiet_window(arguments)
        cron_expr = self._build_schedule_cron_expr(arguments)

        if kind == "daily":
            job_name = f"Feishu daily digest @ {schedule_text}: {arguments['topic']}"
            message = self._build_daily_template_prompt(arguments)
        elif kind == "hotspot":
            job_name = f"Feishu hotspot brief @ {schedule_text}: {arguments['topic']}"
            message = self._build_hotspot_template_prompt(arguments)
        else:
            job_name = f"Feishu paper monitor @ {schedule_text}: {arguments['topic']}"
            message = self._build_paper_template_prompt(arguments)

        return job_name, message, cron_expr, schedule_text, quiet_text

    async def _handle_feedback_command(
        self,
        text: str,
        chat_id: str,
        user_id: str,
    ) -> str:
        """Record chat-scoped workflow feedback for the latest Feishu run."""
        signal, parse_error = self._parse_feedback_command(text)
        if parse_error:
            return parse_error
        if not signal:
            signal = self._parse_feedback_shortcut(text)
        if not signal:
            return ""

        from nanoclaw.security.audit import get_audit_log

        session_id = self._build_session_id(chat_id, user_id)
        audit = get_audit_log()
        try:
            updated = await audit.set_latest_workflow_feedback(session_id, signal)
        except KeyError:
            return (
                "No recent workflow run was found in this chat yet.\n\n"
                "Ask nanoClaw something first, then send "
                "`/feedback positive|neutral|negative`."
            )
        except ValueError as exc:
            return str(exc)

        return (
            f"Recorded `{signal}` feedback for workflow run "
            f"#{updated['workflow_run_id']} ({updated['workflow_name']})."
        )

    async def _handle_workflow_command(
        self,
        text: str,
        chat_id: str = "",
        user_id: str = "",
    ) -> str:
        """Show workflow recommendations or recent evaluations inside Feishu chat."""
        combined_reply = await self._handle_contextual_workflow_feedback_suggest_shortcut(
            text,
            chat_id,
            user_id,
        )
        if combined_reply:
            return combined_reply
        reference_reply = await self._handle_workflow_reference_shortcut(text, chat_id)
        if reference_reply:
            return reference_reply
        parsed, parse_error = self._parse_workflow_command(text)
        if parse_error:
            return parse_error
        if not parsed:
            rewritten_text, rewrite_error = self._rewrite_workflow_command_shortcut(text)
            if rewrite_error:
                return rewrite_error
            if not rewritten_text and chat_id and user_id:
                rewritten_text, rewrite_error = await self._rewrite_contextual_workflow_shortcut(
                    text,
                    chat_id,
                    user_id,
                )
                if rewrite_error:
                    return rewrite_error
            if not rewritten_text:
                return ""
            parsed, parse_error = self._parse_workflow_command(rewritten_text)
            if parse_error:
                return parse_error
            if not parsed:
                return ""

        from nanoclaw.security.audit import get_audit_log
        action = str(parsed.get("action") or "report")
        if action == "suggest":
            item = await get_audit_log().get_workflow_evaluation(
                int(parsed["workflow_run_id"]),
            )
            if item is None:
                return f"Workflow run #{parsed['workflow_run_id']} was not found."
            return "\n".join(self._format_workflow_suggestion_lines(item))
        if action == "feedback":
            try:
                item = await get_audit_log().set_workflow_feedback(
                    int(parsed["workflow_run_id"]),
                    str(parsed["feedback_signal"]),
                )
            except KeyError:
                return f"Workflow run #{parsed['workflow_run_id']} was not found."
            except ValueError as exc:
                return str(exc)
            return (
                f"Workflow run #{item['workflow_run_id']} feedback updated to "
                f"{item['feedback_signal']}."
            )
        if action == "recent":
            items = await get_audit_log().get_recent_workflow_evaluations(
                limit=int(parsed["limit"]),
            )
            label_filter = str(parsed.get("label_filter") or "")
            feedback_filter = str(parsed.get("feedback_filter") or "")
            items = self._filter_recent_workflow_evaluations(
                items,
                label_filter,
                feedback_filter,
            )
            filter_parts: list[str] = []
            if label_filter:
                filter_parts.append(f"label={label_filter}")
            if feedback_filter:
                filter_parts.append(f"feedback={feedback_filter}")
            filter_suffix = f", {', '.join(filter_parts)}" if filter_parts else ""
            if not items:
                return f"No recent workflow evaluations found{filter_suffix}."
            self._remember_workflow_references(chat_id, "recent", items)
            lines = [f"Recent workflow evaluations (limit={parsed['limit']}{filter_suffix}):"]
            for item in items:
                lines.extend(self._format_recent_workflow_evaluation(item))
            return "\n".join(lines)

        try:
            items = await get_audit_log().get_workflow_recommendations(
                days=int(parsed["days"]),
                limit=int(parsed["limit"]),
            )
        except ValueError as exc:
            return str(exc)
        status_filter = str(parsed.get("status_filter") or "")
        feedback_filter = str(parsed.get("feedback_filter") or "")
        items = self._filter_workflow_recommendations(items, status_filter, feedback_filter)
        filter_parts: list[str] = []
        if status_filter:
            filter_parts.append(f"status={status_filter}")
        if feedback_filter:
            filter_parts.append(f"feedback={feedback_filter}")
        filter_suffix = f", {', '.join(filter_parts)}" if filter_parts else ""

        if not items:
            return (
                f"No workflow recommendations found in the last {parsed['days']} day(s)"
                f"{filter_suffix}."
            )

        self._remember_workflow_references(chat_id, "report", items)
        lines = [f"Workflow recommendations ({parsed['days']}d{filter_suffix}):"]
        for item in items:
            lines.append(
                "- "
                f"{item['workflow_name']} [{item['recommendation_status']}] "
                f"runs={item['run_count']} "
                f"quality={item['avg_quality_score']} "
                f"efficiency={item['avg_efficiency_score']}"
            )
            lines.append(
                "  "
                f"feedback={item['positive_feedback']}/"
                f"{item['neutral_feedback']}/"
                f"{item['negative_feedback']}"
            )
            if item.get("recommendations"):
                lines.append(f"  next={item['recommendations'][0]}")
        return "\n".join(lines)

    async def _handle_schedule_command(self, text: str, chat_id: str) -> str:
        """Create/list/remove Feishu schedule templates through cron jobs."""
        parsed, parse_error = self._parse_schedule_command(text)
        if parse_error:
            return parse_error
        scheduler = getattr(self.gateway, "scheduler", None)
        if not parsed:
            rewritten_text, rewrite_error = self._rewrite_schedule_management_shortcut(text)
            if not rewritten_text and not rewrite_error:
                rewritten_text, rewrite_error = self._rewrite_filtered_schedule_management_shortcut(
                    text
                )
            if not rewritten_text and not rewrite_error and scheduler is not None:
                rewritten_text, rewrite_error = await self._rewrite_named_schedule_management_shortcut(
                    text,
                    chat_id,
                    scheduler,
                )
            if not rewritten_text and not rewrite_error:
                rewritten_text, rewrite_error = self._rewrite_natural_schedule_command(text)
            if rewrite_error:
                return rewrite_error
            if not rewritten_text:
                return ""
            logger.info(
                "Feishu natural schedule mapped to command template `%s`",
                rewritten_text,
            )
            parsed, parse_error = self._parse_schedule_command(rewritten_text)
            if parse_error:
                return parse_error
            if not parsed:
                return ""

        if scheduler is None:
            return "Scheduler is not available yet. Start nanoClaw with `nanoclaw serve`."

        action = str(parsed.get("action") or "")
        if action == "list":
            health_filter = str(parsed.get("health_filter") or "")
            signal_filter = str(parsed.get("signal_filter") or "")
            jobs = await self._list_schedule_jobs(scheduler)
            visible = self._filter_chat_schedule_jobs(
                jobs,
                chat_id,
                health_filter=health_filter,
                signal_filter=signal_filter,
            )
            filter_suffix = self._describe_schedule_list_filters(
                health_filter=health_filter,
                signal_filter=signal_filter,
            )
            if not visible:
                return f"No scheduled Feishu jobs for this chat{filter_suffix}."

            lines = [
                f"Scheduled Feishu jobs for this chat{filter_suffix}:",
                "Use the `#ID` at the start of each line with `/schedule show|pause|resume|remove`.",
            ]
            for job in visible:
                lines.append(self._format_schedule_job_list_entry(job))
            return "\n".join(lines)
        if action == "show":
            job_id = int(parsed["job_id"])
            jobs = await self._list_schedule_jobs(scheduler)
            for job in jobs:
                if int(job["id"]) != job_id:
                    continue
                if str(job.get("channel") or "") != "feishu":
                    return f"Job #{job_id} is not a Feishu schedule."
                if str(job.get("target_id") or "") != chat_id:
                    return f"Job #{job_id} does not belong to this chat."
                return self._format_schedule_job_details(job)
            return f"Feishu schedule #{job_id} was not found."

        if action == "remove":
            job_id = int(parsed["job_id"])
            jobs = await self._list_schedule_jobs(scheduler)
            for job in jobs:
                if int(job["id"]) != job_id:
                    continue
                if str(job.get("channel") or "") != "feishu":
                    return f"Job #{job_id} is not a Feishu schedule."
                if str(job.get("target_id") or "") != chat_id:
                    return f"Job #{job_id} does not belong to this chat."
                await scheduler.remove_job(job_id)
                return f"Removed Feishu schedule {self._format_schedule_job_ref(job)}."
            return f"Feishu schedule #{job_id} was not found."
        if action == "remove_matching":
            health_filter = str(parsed.get("health_filter") or "")
            signal_filter = str(parsed.get("signal_filter") or "")
            jobs = await self._list_schedule_jobs(scheduler)
            visible = self._filter_chat_schedule_jobs(
                jobs,
                chat_id,
                health_filter=health_filter,
                signal_filter=signal_filter,
            )
            filter_suffix = self._describe_schedule_list_filters(
                health_filter=health_filter,
                signal_filter=signal_filter,
            )
            if not visible:
                return f"No Feishu schedules in this chat match the current filters{filter_suffix}."
            affected_lines: list[str] = []
            for job in visible:
                job_id = int(job["id"])
                await scheduler.remove_job(job_id)
                affected_lines.append(self._format_schedule_job_ref(job))
            count = len(affected_lines)
            noun = "schedule" if count == 1 else "schedules"
            lines = [f"Removed {count} Feishu {noun} in this chat{filter_suffix}:"]
            lines.extend(f"- {line}" for line in affected_lines)
            return "\n".join(lines)
        if action in {"pause_matching", "resume_matching"}:
            health_filter = str(parsed.get("health_filter") or "")
            signal_filter = str(parsed.get("signal_filter") or "")
            jobs = await self._list_schedule_jobs(scheduler)
            visible = self._filter_chat_schedule_jobs(
                jobs,
                chat_id,
                health_filter=health_filter,
                signal_filter=signal_filter,
            )
            filter_suffix = self._describe_schedule_list_filters(
                health_filter=health_filter,
                signal_filter=signal_filter,
            )
            if not visible:
                return f"No Feishu schedules in this chat match the current filters{filter_suffix}."
            enable = action == "resume_matching"
            affected_lines: list[str] = []
            for job in visible:
                job_id = int(job["id"])
                await scheduler.toggle_job(job_id, enable)
                affected_lines.append(self._format_schedule_job_ref(job))
            count = len(affected_lines)
            noun = "schedule" if count == 1 else "schedules"
            state = "Enabled" if enable else "Paused"
            lines = [f"{state} {count} Feishu {noun} in this chat{filter_suffix}:"]
            lines.extend(f"- {line}" for line in affected_lines)
            return "\n".join(lines)
        if action in {"pause", "resume"}:
            job_id = int(parsed["job_id"])
            jobs = await self._list_schedule_jobs(scheduler)
            for job in jobs:
                if int(job["id"]) != job_id:
                    continue
                if str(job.get("channel") or "") != "feishu":
                    return f"Job #{job_id} is not a Feishu schedule."
                if str(job.get("target_id") or "") != chat_id:
                    return f"Job #{job_id} does not belong to this chat."
                enable = action == "resume"
                await scheduler.toggle_job(job_id, enable)
                state = "enabled" if enable else "paused"
                return (
                    f"{state.capitalize()} Feishu schedule "
                    f"{self._format_schedule_job_ref(job)}."
                )
            return f"Feishu schedule #{job_id} was not found."

        arguments = dict(parsed.get("arguments") or {})
        kind = str(arguments.get("kind") or "")
        job_name, message, cron_expr, schedule_text, quiet_text = self._build_schedule_job_payload(
            arguments
        )
        action = str(parsed.get("action") or "")
        if action == "update":
            job_id = int(parsed["job_id"])
            jobs = await self._list_schedule_jobs(scheduler)
            for job in jobs:
                if int(job["id"]) != job_id:
                    continue
                if str(job.get("channel") or "") != "feishu":
                    return f"Job #{job_id} is not a Feishu schedule."
                if str(job.get("target_id") or "") != chat_id:
                    return f"Job #{job_id} does not belong to this chat."
                await scheduler.update_job(
                    job_id,
                    name=job_name,
                    message=message,
                    cron_expr=cron_expr,
                    interval_seconds=None,
                    channel="feishu",
                    target_id=chat_id,
                    quiet_start=str(arguments.get("quiet_start") or ""),
                    quiet_end=str(arguments.get("quiet_end") or ""),
                )
                return (
                    f"Updated Feishu schedule #{job_id}.\n"
                    f"Name: {job_name}\n"
                    f"Kind: {kind}\n"
                    f"Schedule: {schedule_text}\n"
                    f"Quiet window: {quiet_text}\n"
                    "Delivery: current Feishu chat\n"
                    "Tip: run `/schedule list` to see the current `#ID -> task name` mapping."
                )
            return f"Feishu schedule #{job_id} was not found."

        job_id = await scheduler.add_job(
            job_name,
            message,
            cron_expr=cron_expr,
            channel="feishu",
            target_id=chat_id,
            quiet_start=str(arguments.get("quiet_start") or ""),
            quiet_end=str(arguments.get("quiet_end") or ""),
        )
        return (
            f"Created Feishu schedule #{job_id}.\n"
            f"Name: {job_name}\n"
            f"Kind: {kind}\n"
            f"Schedule: {schedule_text}\n"
            f"Quiet window: {quiet_text}\n"
            "Delivery: current Feishu chat\n"
            "Tip: run `/schedule list` to see the current `#ID -> task name` mapping."
        )

    def _apply_common_templates(self, text: str) -> tuple[str, str]:
        """Apply Feishu common command templates before handing to the agent."""
        stripped = text.strip()
        if not stripped:
            return "", ""
        if not stripped.lower().startswith("/paper"):
            return stripped, ""

        arguments, parse_error = self._parse_paper_command(stripped)
        if parse_error:
            return "", parse_error
        if not arguments:
            return "", self._paper_usage()

        topic = str(arguments.get("topic", "")).strip()
        logger.info("Feishu template `/paper` mapped to paper_search topic `%s`", topic)
        return self._build_paper_template_prompt(arguments), ""

    def _prune_pending_confirmations(self) -> None:
        """Drop old pending confirmations and cap in-memory size."""
        now = time.time()
        for confirm_id in list(self._pending_confirmations.keys()):
            pending = self._pending_confirmations[confirm_id]
            if now - pending.created_at > 600:
                if not pending.future.done():
                    pending.future.set_result(False)
                del self._pending_confirmations[confirm_id]

        if len(self._pending_confirmations) <= 256:
            return

        oldest_ids = sorted(
            self._pending_confirmations.keys(),
            key=lambda item: self._pending_confirmations[item].created_at,
        )
        for confirm_id in oldest_ids[: len(self._pending_confirmations) - 256]:
            pending = self._pending_confirmations.pop(confirm_id, None)
            if pending and not pending.future.done():
                pending.future.set_result(False)

    async def _ask_confirmation(self, chat_id: str, user_id: str, question: str) -> bool:
        """Ask user for confirmation inside Feishu chat."""
        if not chat_id:
            return False

        self._prune_pending_confirmations()
        confirm_id = uuid.uuid4().hex[:8]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending_confirmations[confirm_id] = PendingConfirmation(
            user_id=user_id,
            chat_id=chat_id,
            future=future,
            created_at=time.time(),
        )

        prompt = (
            f"{question}\n\n"
            f"Reply with `yes {confirm_id}` to approve or `no {confirm_id}` to deny. "
            "Expires in 5 minutes."
        )
        sent = await self._send_text_to_chat(chat_id, prompt)
        if not sent:
            self._pending_confirmations.pop(confirm_id, None)
            return False

        try:
            return await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending_confirmations.pop(confirm_id, None)

    async def _try_consume_confirmation_reply(
        self,
        chat_id: str,
        user_id: str,
        text: str,
    ) -> bool:
        """
        Consume a yes/no reply for an outstanding confirmation request.

        Returns:
            True if message was consumed as a confirmation reply.
        """
        self._prune_pending_confirmations()
        match = re.match(r"^(yes|no)\s+([0-9a-fA-F-]{8,})$", text.strip(), re.IGNORECASE)
        if not match:
            return False

        decision = match.group(1).lower()
        confirm_id = match.group(2).lower()
        pending = self._pending_confirmations.get(confirm_id)
        if not pending:
            if chat_id:
                await self._send_text_to_chat(
                    chat_id,
                    f"Confirmation `{confirm_id}` was not found or already expired.",
                )
            return True

        if pending.user_id != user_id:
            if chat_id:
                await self._send_text_to_chat(
                    chat_id,
                    "You are not authorized for this confirmation.",
                )
            return True

        if pending.chat_id and pending.chat_id != chat_id:
            if chat_id:
                await self._send_text_to_chat(chat_id, "Please reply in the original chat.")
            return True

        if not pending.future.done():
            pending.future.set_result(decision == "yes")

        del self._pending_confirmations[confirm_id]
        if chat_id:
            action = "approved" if decision == "yes" else "denied"
            await self._send_text_to_chat(chat_id, f"Confirmation `{confirm_id}` {action}.")
        return True

    async def _handle_event(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        """Handle Feishu event callback payloads."""
        try:
            payload = await request.json()
        except Exception:
            return aiohttp.web.json_response({"code": 1, "msg": "invalid json"}, status=400)

        if payload.get("type") == "url_verification":
            if self.config.verify_token and payload.get("token") != self.config.verify_token:
                logger.warning(
                    "Feishu url_verification token mismatch. "
                    "Align Feishu Event Subscription Verification Token with "
                    "`channels.feishu.verifyToken` in ~/.nanoclaw/config.json "
                    "(expected %s, provided %s).",
                    self._describe_token(self.config.verify_token),
                    self._describe_token(str(payload.get("token", ""))),
                )
                return aiohttp.web.json_response({"code": 1, "msg": "invalid token"}, status=403)
            return aiohttp.web.json_response({"challenge": payload.get("challenge", "")})

        if payload.get("encrypt"):
            logger.warning(
                "Feishu sent an encrypted event, but nanoClaw does not support encrypted "
                "callbacks yet. Disable encryption in Feishu Event Subscription settings."
            )
            return aiohttp.web.json_response(
                {"code": 1, "msg": "encrypted events not supported"},
                status=400,
            )

        header = payload.get("header", {})
        event = payload.get("event", {})
        event_id = str(header.get("event_id", ""))
        event_type = str(header.get("event_type", ""))
        header_token = str(header.get("token", ""))

        if self.config.verify_token and header_token and header_token != self.config.verify_token:
            logger.warning(
                "Feishu event header token mismatch. "
                "Align Feishu Event Subscription Verification Token with "
                "`channels.feishu.verifyToken` in ~/.nanoclaw/config.json "
                "(expected %s, provided %s).",
                self._describe_token(self.config.verify_token),
                self._describe_token(header_token),
            )
            return aiohttp.web.json_response({"code": 1, "msg": "invalid token"}, status=403)

        if not self._mark_seen_event(event_id):
            return aiohttp.web.json_response({"code": 0})

        if event_type != "im.message.receive_v1":
            return aiohttp.web.json_response({"code": 0})

        sender = event.get("sender", {})
        if not self._is_allowed_sender(sender):
            return aiohttp.web.json_response({"code": 0})

        message = event.get("message", {})
        if message.get("message_type") != "text":
            return aiohttp.web.json_response({"code": 0})

        text = self._extract_text(message.get("content", "{}"))
        if not text:
            return aiohttp.web.json_response({"code": 0})

        user_id = self._resolve_sender_id(sender)
        message_id = str(message.get("message_id", ""))
        chat_id = str(message.get("chat_id", ""))

        if await self._try_consume_confirmation_reply(chat_id, user_id, text):
            return aiohttp.web.json_response({"code": 0})

        workflow_reply = await self._handle_workflow_command(text, chat_id, user_id)
        if workflow_reply:
            if message_id:
                await self._reply_to_message(message_id, workflow_reply)
            elif chat_id:
                await self._send_text_to_chat(chat_id, workflow_reply)
            return aiohttp.web.json_response({"code": 0})

        feedback_reply = await self._handle_feedback_command(text, chat_id, user_id)
        if feedback_reply:
            if message_id:
                await self._reply_to_message(message_id, feedback_reply)
            elif chat_id:
                await self._send_text_to_chat(chat_id, feedback_reply)
            return aiohttp.web.json_response({"code": 0})

        schedule_reply = await self._handle_schedule_command(text, chat_id)
        if schedule_reply:
            if message_id:
                await self._reply_to_message(message_id, schedule_reply)
            elif chat_id:
                await self._send_text_to_chat(chat_id, schedule_reply)
            return aiohttp.web.json_response({"code": 0})

        templated_message, template_reply = self._apply_common_templates(text)
        if template_reply:
            if message_id:
                await self._reply_to_message(message_id, template_reply)
            elif chat_id:
                await self._send_text_to_chat(chat_id, template_reply)
            return aiohttp.web.json_response({"code": 0})
        if not templated_message:
            return aiohttp.web.json_response({"code": 0})

        response = await self.gateway.handle_incoming(
            channel_id="feishu",
            user_id=self._build_session_user_id(chat_id, user_id),
            message=templated_message,
            confirm_callback=lambda q: self._ask_confirmation(chat_id, user_id, q),
        )

        if message_id:
            await self._reply_to_message(message_id, response)
        elif chat_id:
            await self._send_text_to_chat(chat_id, response)

        return aiohttp.web.json_response({"code": 0})
