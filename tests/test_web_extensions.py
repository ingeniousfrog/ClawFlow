"""Tests for paper search and WeChat writing helper functions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nanoclaw.security.sandbox import FileGuard, set_file_guard
from nanoclaw.tools.web import (
    _assign_quality_tier,
    _build_arxiv_query,
    _build_wechat_article_role_sections,
    _build_wechat_article_sections,
    _compute_paper_trend_signals,
    _dedupe_papers,
    _export_wechat_article_bundle,
    _extract_arxiv_id_from_url,
    _markdown_to_basic_html,
    _parse_paper_providers,
    _parse_arxiv_atom,
    _parse_arxiv_categories,
    _select_arxiv_entries,
    _sort_papers,
    _summarize_evidence_verification,
    _write_workspace_file,
    _verification_status_label,
)


SAMPLE_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2503.00001v1</id>
    <updated>2026-03-03T10:00:00Z</updated>
    <published>2026-03-03T09:00:00Z</published>
    <title>Agentic Multimodal Planning for Robotics</title>
    <summary>We present a planner for multimodal robotic agents.</summary>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
    <category term="cs.AI" />
    <category term="cs.RO" />
    <link rel="alternate" href="https://arxiv.org/abs/2503.00001" />
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2503.00002v1</id>
    <updated>2026-03-01T10:00:00Z</updated>
    <published>2026-03-01T09:00:00Z</published>
    <title>Benchmarking LLM-based Scientific Discovery</title>
    <summary>We benchmark automated hypothesis generation.</summary>
    <author><name>Charlie</name></author>
    <category term="cs.AI" />
    <link rel="alternate" href="https://arxiv.org/abs/2503.00002" />
  </entry>
</feed>
"""


def test_parse_arxiv_categories_filters_invalid_values() -> None:
    """Category parser should keep valid values and drop invalid ones."""
    parsed = _parse_arxiv_categories("cs.AI, quant-ph, bad/category,cs.AI")
    assert parsed == ["cs.AI", "quant-ph"]


def test_build_arxiv_query_includes_categories() -> None:
    """arXiv query should include both terms and category constraints."""
    query, terms = _build_arxiv_query("latest ai agents", ["cs.AI", "cs.LG"])
    assert terms
    assert "cat:cs.AI" in query
    assert "cat:cs.LG" in query
    assert 'all:"' in query


def test_parse_arxiv_atom_and_trend_signals() -> None:
    """Atom parser should extract entries and support trend computation."""
    entries = _parse_arxiv_atom(SAMPLE_ARXIV_ATOM)
    assert len(entries) == 2
    assert entries[0]["title"].startswith("Agentic Multimodal")
    assert entries[0]["url"].startswith("https://arxiv.org/abs/")

    selected, in_window = _select_arxiv_entries(entries, recency_days=30, max_items=5)
    assert in_window is True
    trend = _compute_paper_trend_signals(selected)
    assert trend["confidence"] in {"low", "medium", "high"}
    assert trend["dominant_categories"]


def test_wechat_sections_include_full_pipeline() -> None:
    """WeChat helper should generate all writing stages."""
    evidence_items = [
        {
            "kind": "paper",
            "source": "arXiv",
            "title": "Fast Video Diffusion with Cached Attention",
            "url": "https://arxiv.org/abs/2603.11111",
            "snippet": "We reduce inference latency with attention cache reuse.",
            "published": "2026-03-02T10:00:00Z",
            "verify_status": "ok",
            "verify_http": "200",
            "verify_note": "reachable",
            "verify_final_url": "https://arxiv.org/abs/2603.11111",
        },
        {
            "kind": "news",
            "source": "TechCrunch",
            "title": "New startup ships real-time video generation stack",
            "url": "https://example.com/news-1",
            "snippet": "The team reports strong throughput gains in production.",
            "published": "2026-03-03T09:00:00Z",
            "verify_status": "failed",
            "verify_http": "403",
            "verify_note": "http_403",
            "verify_final_url": "https://example.com/news-1",
        },
    ]
    sections = _build_wechat_article_sections(
        topic="AI Agent 行业观察",
        audience="产品经理",
        goal="输出周报",
        style="专业",
        length="medium",
        evidence_items=evidence_items,
        evidence_status="Evidence scope: user=0, rss=1, paper=1, selected=2",
    )
    assert {"topic", "outline", "draft", "factcheck", "polish", "export"} <= set(sections.keys())
    assert "来源URL" in sections["factcheck"]
    assert "关键变化拆解" in sections["draft"]
    assert "核查摘要" in sections["draft"]
    assert "可达" in sections["factcheck"]
    assert "403" in sections["factcheck"]

    role_sections = _build_wechat_article_role_sections(
        topic="AI Agent 行业观察",
        audience="产品经理",
        goal="输出周报",
        style="专业",
        length="medium",
        evidence_items=evidence_items,
        evidence_status="Evidence scope: user=0, rss=1, paper=1, selected=2",
        sections=sections,
    )
    assert {
        "role_chain",
        "planner",
        "researcher",
        "drafter",
        "critic",
        "editor",
    } <= set(role_sections.keys())
    assert "planner -> researcher -> drafter -> critic -> editor" in role_sections["role_chain"]
    assert "标题候选" in role_sections["planner"]
    assert "证据池" in role_sections["researcher"]
    assert "关键变化拆解" in role_sections["drafter"]
    assert "最终编辑要求" in role_sections["editor"]


def test_verification_summary_and_label() -> None:
    """Verification helpers should produce stable labels and summary."""
    evidence_items = [
        {"verify_status": "ok"},
        {"verify_status": "failed"},
        {"verify_status": "blocked"},
        {"verify_status": "invalid"},
        {"verify_status": "skipped"},
    ]
    summary = _summarize_evidence_verification(evidence_items)
    assert "ok=1" in summary
    assert "failed=1" in summary
    assert "blocked=1" in summary
    assert "invalid=1" in summary
    assert "skipped=1" in summary
    assert _verification_status_label("ok") == "可达"
    assert _verification_status_label("failed") == "失败"


def test_markdown_to_basic_html_renders_common_blocks() -> None:
    """Markdown converter should keep structure for headings/lists/links/tables."""
    markdown_text = (
        "# Weekly Report\n\n"
        "Intro with **bold** and [source](https://example.com).\n\n"
        "- item a\n"
        "- item b\n\n"
        "| name | value |\n"
        "| --- | --- |\n"
        "| speed | fast |\n"
    )
    html_text = _markdown_to_basic_html(markdown_text)
    assert "<h1>Weekly Report</h1>" in html_text
    assert "<strong>bold</strong>" in html_text
    assert '<a href="https://example.com">source</a>' in html_text
    assert "<ul>" in html_text and "<li>item a</li>" in html_text
    assert "<table>" in html_text and "<th>name</th>" in html_text


def test_export_wechat_article_bundle_writes_files(tmp_path: Path) -> None:
    """Export helper should write markdown and html files into workspace."""
    set_file_guard(FileGuard(tmp_path))
    sections = {
        "topic": "T",
        "outline": "O",
        "draft": "D",
        "factcheck": "F",
        "polish": "P",
        "export": "E",
    }
    result = _export_wechat_article_bundle(
        topic="Video Weekly",
        sections=sections,
        status_line="Evidence scope: selected=2",
    )
    assert result["ok"] == "true"

    md_path = Path(result["md_path"])
    html_path = Path(result["html_path"])
    assert md_path.exists()
    assert html_path.exists()
    assert md_path.suffix == ".md"
    assert html_path.suffix == ".html"
    assert "## draft" in md_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html_path.read_text(encoding="utf-8")


def test_write_workspace_file_blocks_sensitive_target(tmp_path: Path) -> None:
    """Web helpers should reuse the shared file boundary policy for writes."""
    set_file_guard(FileGuard(tmp_path))
    ok, message = _write_workspace_file(".env.local", "secret=true")
    assert ok is False
    assert "ACCESS DENIED" in message


def test_extract_arxiv_id_from_url() -> None:
    """arXiv abs URL parser should extract identifier."""
    assert _extract_arxiv_id_from_url("https://arxiv.org/abs/2603.02883") == "2603.02883"
    assert _extract_arxiv_id_from_url("https://arxiv.org/abs/2603.02883v2?ref=x") == "2603.02883v2"
    assert _extract_arxiv_id_from_url("https://example.com/x") == ""


def test_parse_paper_providers_with_aliases() -> None:
    """Provider parser should support aliases and defaults."""
    assert _parse_paper_providers("") == ["arxiv", "openalex", "semantic_scholar"]
    assert _parse_paper_providers("arxiv,s2,semantic-scholar") == [
        "arxiv",
        "semantic_scholar",
    ]
    assert _parse_paper_providers("invalid") == ["arxiv", "openalex", "semantic_scholar"]


def test_dedupe_papers_prefers_journal_version() -> None:
    """Dedup should keep richer journal metadata over preprint-only metadata."""
    published_dt = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
    arxiv = {
        "title": "Fast Diffusion Distillation for Video Generation",
        "summary": "preprint summary",
        "url": "https://arxiv.org/abs/2603.11111",
        "published": "2026-03-02T10:00:00Z",
        "published_dt": published_dt,
        "authors": ["Alice", "Bob"],
        "institutions": [],
        "categories": ["cs.CV"],
        "venue": "arXiv",
        "citations": 0,
        "doi": "",
        "arxiv_id": "2603.11111",
        "source": "arxiv",
        "source_type": "preprint",
        "sources": ["arXiv"],
        "quality_tier": "",
    }
    journal = {
        "title": "Fast Diffusion Distillation for Video Generation",
        "summary": "journal abstract",
        "url": "https://doi.org/10.1234/vgen.2026.1",
        "published": "2026-03-03",
        "published_dt": datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc),
        "authors": ["Alice", "Bob", "Carol"],
        "institutions": ["Tsinghua University"],
        "categories": [],
        "venue": "ACM MM",
        "citations": 25,
        "doi": "10.1234/vgen.2026.1",
        "arxiv_id": "2603.11111",
        "source": "openalex",
        "source_type": "journal",
        "sources": ["OpenAlex"],
        "quality_tier": "",
    }

    merged = _dedupe_papers([arxiv, journal])
    assert len(merged) == 1
    assert merged[0]["source"] == "openalex"
    assert merged[0]["citations"] == 25
    assert "arXiv" in merged[0]["sources"]
    assert "OpenAlex" in merged[0]["sources"]
    assert merged[0]["venue"] == "ACM MM"


def test_quality_tier_and_sort_dimensions() -> None:
    """Paper sort should support citation/author/institution dimensions."""
    papers = [
        {
            "title": "Paper A",
            "summary": "",
            "url": "https://a",
            "published": "2026-03-01",
            "published_dt": datetime(2026, 3, 1, tzinfo=timezone.utc),
            "authors": ["Alice Zhang"],
            "institutions": ["PKU"],
            "categories": [],
            "venue": "Nature",
            "citations": 120,
            "doi": "10.1/a",
            "arxiv_id": "",
            "source": "openalex",
            "source_type": "journal",
            "sources": ["OpenAlex"],
            "quality_tier": "",
        },
        {
            "title": "Paper B",
            "summary": "",
            "url": "https://b",
            "published": "2026-03-04",
            "published_dt": datetime(2026, 3, 4, tzinfo=timezone.utc),
            "authors": ["Bob Li"],
            "institutions": ["Fudan University"],
            "categories": [],
            "venue": "arXiv",
            "citations": 2,
            "doi": "",
            "arxiv_id": "2603.2",
            "source": "arxiv",
            "source_type": "preprint",
            "sources": ["arXiv"],
            "quality_tier": "",
        },
        {
            "title": "Paper C",
            "summary": "",
            "url": "https://c",
            "published": "2026-02-20",
            "published_dt": datetime(2026, 2, 20, tzinfo=timezone.utc),
            "authors": ["Carol Wang"],
            "institutions": ["Tsinghua University"],
            "categories": [],
            "venue": "NeurIPS",
            "citations": 20,
            "doi": "10.1/c",
            "arxiv_id": "",
            "source": "semantic_scholar",
            "source_type": "journal",
            "sources": ["Semantic Scholar"],
            "quality_tier": "",
        },
    ]
    for paper in papers:
        paper["quality_tier"] = _assign_quality_tier(paper)

    assert papers[0]["quality_tier"] in {"A", "B"}
    assert papers[1]["quality_tier"] == "D"
    assert papers[2]["quality_tier"] == "C"

    by_citation = _sort_papers(papers, sort_by="citation", author="", institution="")
    assert by_citation[0]["title"] == "Paper A"

    by_author = _sort_papers(papers, sort_by="author", author="carol", institution="")
    assert by_author[0]["title"] == "Paper C"

    by_institution = _sort_papers(
        papers,
        sort_by="institution",
        author="",
        institution="fudan",
    )
    assert by_institution[0]["title"] == "Paper B"
