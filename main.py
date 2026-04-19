"""
Colorectal Weekly Digest Bot
- 大腸分野に特化した週次文献まとめ
- PubMedから直近7日のCRC/ポリープ/EMR-ESD/大腸内視鏡関連論文を取得
- Claude Sonnetで個別要約+週次トレンド俯瞰を生成
- Discordに3セクション構成で投稿
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# shared/ を import パスに追加
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared import pubmed, claude_client, notify, history  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402

logger = setup_logging("colorectal-digest")

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "7"))
MAX_PAPERS = int(os.environ.get("MAX_PAPERS", "60"))
TOP_N_HIGHLIGHT = int(os.environ.get("TOP_N_HIGHLIGHT", "5"))

ARTIFACT_DIR = str(ROOT / "artifacts" / "colorectal_digest")

# 大腸分野の網羅的クエリ
PUBMED_QUERY = (
    '('
    '"Colorectal Neoplasms"[MeSH] OR "colorectal cancer"[tiab] '
    'OR "colorectal neoplasm*"[tiab] OR "colon cancer"[tiab] '
    'OR "rectal cancer"[tiab] OR "rectal neoplasm*"[tiab] '
    'OR "colonic polyp*"[tiab] OR "colorectal polyp*"[tiab] '
    'OR "adenomatous polyp*"[tiab] OR "sessile serrated"[tiab] '
    'OR "colonoscopy"[MeSH] OR "colonoscopy"[tiab] '
    'OR "Endoscopic Mucosal Resection"[MeSH] '
    'OR "Endoscopic Submucosal Dissection"[tiab] '
    'OR ("EMR"[tiab] AND colorectal[tiab]) '
    'OR ("ESD"[tiab] AND (colorectal[tiab] OR colon[tiab] OR rectal[tiab])) '
    'OR "fecal occult blood"[tiab] OR "FIT test"[tiab] '
    'OR "colorectal cancer screening"[tiab] '
    'OR "Lynch syndrome"[MeSH] OR "familial adenomatous polyposis"[tiab]'
    ') '
    'AND English[lang] AND hasabstract[text]'
)


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------

@dataclass
class Summary:
    paper: pubmed.Paper
    summary_ja: str
    importance: int  # 1-5
    category: str    # CRC treatment, endoscopy, screening, polyposis, etc.


# -----------------------------------------------------------------------------
# Claude prompts
# -----------------------------------------------------------------------------

SUMMARY_SYSTEM = """あなたは大腸分野を専門とする消化器内科医向けに英文論文を要約するアシスタントです。
以下のJSON形式のみで返答してください(コードフェンス・前置き禁止)。

{
  "line1": "【研究デザイン】被験者・主要評価項目を簡潔に(60字以内)",
  "line2": "主要結果。具体的な数値(OR/HR/p値/%/sensitivity等)を必ず含める(90字以内)",
  "line3": "臨床的インパクトまたは既存知見との差分(60字以内)",
  "importance": 1-5の整数。5=プラクティス変革級、4=重要新知見、3=確認/補強、2=小規模/限定的、1=症例報告/低impact,
  "category": "以下から1つ選択: CRC_treatment / CRC_screening / endoscopy_ESD_EMR / polyposis / pathology_molecular / surgery / basic_science / other"
}

専門用語は正確な日本語訳を用い、曖昧な表現は避けてください。"""


OVERVIEW_SYSTEM = """あなたは大腸分野を専門とする消化器内科医で、過去1週間の大腸領域の論文群から
「今週のトレンド」を俯瞰するシニアレビュアーです。

以下の観点を網羅し、500字以内の日本語でまとめてください:
1. CRC化学療法・免疫療法(FOLFOX/FOLFIRI、ICI、KRAS G12C阻害薬、MRD-ctDNA等)
2. 内視鏡診断・治療(ESD/EMR技術、JNET/NICE/Kudo分類、cold snare、AI-assisted colonoscopy)
3. 検診・サーベイランス(FIT、大腸内視鏡検診、interval cancer、surveillance間隔)
4. 分子病態・ポリポーシス(Lynch症候群、FAP、CMS分類、MSI/MSS、SSL pathway)
5. 外科・合併症

重要論文は末尾に(著者 et al., 誌名, PMID)形式で明示してください。
読み物として面白くなるよう、事実の羅列ではなくテーマごとの潮流として論じてください。"""


def summarize_paper(paper: pubmed.Paper) -> Summary | None:
    user = (
        f"Title: {paper.title}\n"
        f"Journal: {paper.journal}\n"
        f"Publication types: {', '.join(paper.pub_types or [])}\n"
        f"Abstract: {paper.abstract[:4000]}"
    )
    try:
        data = claude_client.call_json(
            system=SUMMARY_SYSTEM, user=user,
            model=MODEL, max_tokens=500, temperature=0.2,
        )
        summary = f"{data['line1']}\n{data['line2']}\n{data['line3']}"
        return Summary(
            paper=paper,
            summary_ja=summary,
            importance=int(data.get("importance", 3)),
            category=data.get("category", "other"),
        )
    except Exception as e:
        logger.warning(f"Summary failed PMID {paper.pmid}: {e}")
        return None


def generate_overview(summaries: list[Summary]) -> str:
    sorted_s = sorted(summaries, key=lambda s: s.importance, reverse=True)
    lines = []
    for s in sorted_s[:50]:  # コンテキスト節約
        lines.append(
            f"[PMID {s.paper.pmid}] ({s.paper.journal}) [{s.category}] {s.paper.title}\n"
            f"要約: {s.summary_ja}\n重要度: {s.importance}/5"
        )
    user = "以下が今週の大腸分野の論文要約集です:\n\n" + "\n\n".join(lines)

    return claude_client.call(
        system=OVERVIEW_SYSTEM, user=user,
        model=MODEL, max_tokens=1500, temperature=0.4,
    )


# -----------------------------------------------------------------------------
# Discord posting
# -----------------------------------------------------------------------------

CATEGORY_EMOJI = {
    "CRC_treatment": "💊",
    "CRC_screening": "🔍",
    "endoscopy_ESD_EMR": "🔬",
    "polyposis": "🧬",
    "pathology_molecular": "🧪",
    "surgery": "🏥",
    "basic_science": "🔭",
    "other": "📄",
}


def post_results(overview: str, summaries: list[Summary], period_label: str):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Header
    notify.post(
        content=f"🩺 **Colorectal Weekly Digest — {period_label}**\n"
                f"解析対象: {len(summaries)}本  |  生成日: {today}"
    )

    # 1. Trend overview
    notify.post_embed(
        title="📊 今週のトレンド俯瞰",
        description=overview,
        color=0x2E86AB,
    )

    # 2. Top highlights
    top = sorted(
        summaries,
        key=lambda s: (s.importance, s.paper.is_high_impact),
        reverse=True,
    )[:TOP_N_HIGHLIGHT]

    for i, s in enumerate(top, 1):
        p = s.paper
        emoji = CATEGORY_EMOJI.get(s.category, "📄")
        description = (
            f"**{p.title}**\n"
            f"*{p.journal}*  |  {p.first_author_str}\n\n"
            f"{s.summary_ja}\n\n"
            f"[PubMed]({p.url})"
            + (f"  |  [DOI](https://doi.org/{p.doi})" if p.doi else "")
        )
        notify.post_embed(
            title=f"{emoji} 注目論文 #{i}  (importance {s.importance}/5)",
            description=description,
            color=0xE63946,
        )

    # 3. その他リスト(カテゴリ別)
    top_pmids = {s.paper.pmid for s in top}
    others = [s for s in summaries if s.paper.pmid not in top_pmids]
    if others:
        by_cat: dict[str, list[Summary]] = {}
        for s in others:
            by_cat.setdefault(s.category, []).append(s)

        for cat in ["CRC_treatment", "CRC_screening", "endoscopy_ESD_EMR",
                    "polyposis", "pathology_molecular", "surgery",
                    "basic_science", "other"]:
            items = by_cat.get(cat, [])
            if not items:
                continue
            emoji = CATEGORY_EMOJI.get(cat, "📄")
            lines = [
                f"• [{s.paper.journal[:25]}] {s.paper.title[:100]} — [PMID {s.paper.pmid}]({s.paper.url})"
                for s in items
            ]
            notify.post_embed(
                title=f"{emoji} {cat.replace('_', ' ')} ({len(items)})",
                description="\n".join(lines),
                color=0x6C757D,
            )


# -----------------------------------------------------------------------------
# Artifact dump
# -----------------------------------------------------------------------------

def dump_artifacts(summaries: list[Summary], overview: str, period_label: str):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    history.save_json_artifact(
        f"{ARTIFACT_DIR}/digest_{date}.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": period_label,
            "overview": overview,
            "papers": [
                {**asdict(s.paper), "summary_ja": s.summary_ja,
                 "importance": s.importance, "category": s.category}
                for s in summaries
            ],
        },
    )

    # Markdown版
    md_lines = [f"# Colorectal Weekly Digest — {period_label}\n"]
    md_lines.append(f"Generated: {date}  |  Total: {len(summaries)} papers\n")
    md_lines.append("## 今週のトレンド俯瞰\n")
    md_lines.append(overview + "\n")
    md_lines.append("## 論文一覧(重要度順)\n")
    for s in sorted(summaries, key=lambda x: x.importance, reverse=True):
        p = s.paper
        md_lines.append(f"### [{s.category}] {p.title}")
        md_lines.append(f"- **{p.journal}** | importance {s.importance}/5 | "
                        f"[PMID {p.pmid}]({p.url})")
        md_lines.append(f"- {p.first_author_str}\n")
        md_lines.append(s.summary_ja + "\n")
    history.save_artifact(f"{ARTIFACT_DIR}/digest_{date}.md", "\n".join(md_lines))


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main():
    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=DAYS_BACK)
    period_label = f"{date_from:%Y/%m/%d} – {date_to:%Y/%m/%d}"
    logger.info(f"Period: {period_label}")

    # 1. Fetch
    papers = pubmed.recent_days(PUBMED_QUERY, DAYS_BACK, retmax=MAX_PAPERS)
    if not papers:
        notify.post(content=f"⚠️ Colorectal Digest: 期間 {period_label} で該当論文なし")
        return

    # 高インパクト誌を先頭に(上限超過時の優先)
    papers = pubmed.sort_by_impact(papers)

    # 2. Summarize
    summaries: list[Summary] = []
    for i, p in enumerate(papers, 1):
        logger.info(f"Summarizing {i}/{len(papers)}: PMID {p.pmid}")
        s = summarize_paper(p)
        if s:
            summaries.append(s)

    if not summaries:
        notify.post(content="⚠️ Colorectal Digest: 要約生成に全て失敗")
        return

    # 3. Overview
    logger.info("Generating overview...")
    overview = generate_overview(summaries)

    # 4. Post & dump
    post_results(overview, summaries, period_label)
    dump_artifacts(summaries, overview, period_label)
    logger.info("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("Fatal error")
        notify.post_error(e, "Colorectal Digest Bot")
        sys.exit(1)
