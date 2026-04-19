"""
Colorectal Daily Lecture Bot
- 大腸分野特化の毎日講義配信
- Claude Sonnetが履歴を参照してテーマ自動選定
- PubMedで参考文献を取得して本文生成に注入
- 専門医レベル約1000字の日本語講義をDiscord投稿
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared import pubmed, claude_client, notify, history  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402

logger = setup_logging("colorectal-lecture")

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
TARGET_CHARS = int(os.environ.get("TARGET_CHARS", "1000"))
CHAR_TOLERANCE = int(os.environ.get("CHAR_TOLERANCE", "150"))
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "90"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "360"))
N_REFS = int(os.environ.get("N_REFS", "5"))

ARTIFACT_DIR = str(ROOT / "artifacts" / "colorectal_lecture")
HISTORY_FILE = f"{ARTIFACT_DIR}/covered_topics.json"

TOPIC_DOMAINS = [
    "CRC分子病態(CMS classification, CIMP, WNT/APC, p53)",
    "MSI/MMR deficient CRC と免疫療法",
    "KRAS/NRAS/BRAF 変異と分子標的治療",
    "HER2陽性CRC・その他の新規標的",
    "Stage III adjuvant chemotherapy (IDEA trial以降)",
    "転移性CRCの1st/2nd line戦略",
    "oligometastatic CRC(肝転移・肺転移)",
    "直腸癌のTNT(total neoadjuvant therapy)・non-operative management",
    "CRC screening戦略(FIT, colonoscopy, ctDNA test)",
    "ポリープ病理分類と切除適応",
    "ESD手技・デバイス・合併症マネジメント",
    "Cold snare polypectomy vs hot snare",
    "JNET/NICE/Kudo/Paris分類と光学診断",
    "sessile serrated lesion (SSL) とserrated pathway",
    "interval cancer, post-colonoscopy CRC",
    "Surveillance colonoscopy間隔(ESGE/USMSTF)",
    "Lynch症候群診断・surveillance",
    "FAP / MUTYH関連ポリポーシス",
    "AI-assisted colonoscopy(CADe, CADx)",
    "腸内細菌とCRC発癌",
    "ctDNA・MRD monitoring",
    "大腸肛門解剖・神経温存手術",
    "直腸癌TME・側方郭清",
    "CRC術後合併症(縫合不全、LARS)",
    "大腸憩室症・憩室炎管理",
    "IBS・便秘の診断治療",
    "腸管感染症(CDI等)",
]


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------

@dataclass
class TopicPlan:
    topic_ja: str
    topic_en_query: str
    domain: str
    outline: list[str]
    rationale: str


@dataclass
class Lecture:
    plan: TopicPlan
    body: str
    key_points: list[str]
    references: list[pubmed.Paper]


# -----------------------------------------------------------------------------
# Topic selection
# -----------------------------------------------------------------------------

TOPIC_SYSTEM = """あなたは大腸分野を専門とする消化器内科のシニアスタッフで、
同僚の消化器内科専門医に向けて毎日1000字の講義を配信しています。
本日取り上げるテーマを1つ選定してください。

【制約】
- 過去にカバー済みのテーマは避け、切り口を変えてください(同じ薬剤・同じ病態でも視点を変える工夫を)
- 専門医レベルを前提とし、基礎すぎるテーマ(「大腸癌とは」など)は避けてください
- 機序・最新エビデンス・臨床判断のいずれかに明確なフックがあるテーマを選んでください
- 候補ドメインは参考、必須ではありません

【出力形式】
必ず以下のJSONのみを返してください(コードフェンス・前置き禁止):
{
  "topic_ja": "講義タイトル(日本語、40字以内、具体的に)",
  "topic_en_query": "PubMed検索用の英語クエリ(MeSH風、5-10語)",
  "domain": "候補ドメインから最も近いもの、または新規ドメイン名",
  "outline": ["項目1", "項目2", "項目3", "項目4", "項目5"],
  "rationale": "なぜ今このテーマか(60字以内)"
}"""


def select_topic(covered: list[dict]) -> TopicPlan:
    covered_text = "\n".join(
        f"- [{e.get('date', '?')[:10]}] ({e.get('domain', '?')}) {e.get('topic_ja', '?')}"
        for e in covered[-60:]
    ) or "(履歴なし)"
    domains_text = "\n".join(f"- {d}" for d in TOPIC_DOMAINS)

    user = (
        f"【候補ドメイン】\n{domains_text}\n\n"
        f"【過去{HISTORY_DAYS}日間にカバー済みのテーマ】\n{covered_text}\n\n"
        f"本日の講義テーマを1つ選定してください。"
    )

    data = claude_client.call_json(
        system=TOPIC_SYSTEM, user=user,
        model=MODEL, max_tokens=800, temperature=0.8,
    )
    plan = TopicPlan(
        topic_ja=data["topic_ja"],
        topic_en_query=data["topic_en_query"],
        domain=data.get("domain", "未分類"),
        outline=data.get("outline", []),
        rationale=data.get("rationale", ""),
    )
    logger.info(f"Topic: {plan.topic_ja} (domain={plan.domain})")
    return plan


# -----------------------------------------------------------------------------
# Reference gathering
# -----------------------------------------------------------------------------

def fetch_references(query: str, n: int) -> list[pubmed.Paper]:
    """大腸関連かつ直近5年の論文から参考文献を選定"""
    full_query = (
        f"({query}) AND "
        f'("Colorectal Neoplasms"[MeSH] OR "colorectal"[tiab] OR "colon"[tiab] '
        f'OR "rectal"[tiab] OR "colonoscopy"[tiab] OR "polyp*"[tiab]) '
        f"AND English[lang] AND hasabstract[text]"
    )
    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=365 * 5)

    papers = pubmed.search_and_fetch(
        query=full_query,
        mindate=date_from, maxdate=date_to,
        retmax=n * 3, sort="relevance",
    )
    return pubmed.sort_by_impact(papers)[:n]


# -----------------------------------------------------------------------------
# Lecture generation
# -----------------------------------------------------------------------------

LECTURE_SYSTEM = """あなたは大腸分野を専門とする消化器内科医で、同僚の消化器内科専門医に向けて
毎日1000字の講義を配信しています。

【文体・レベル要件】
- 対象は消化器内科専門医。基礎的説明は省き、機序・エビデンス・臨床判断に踏み込む
- 分子機序(シグナル経路、遺伝子変異、タンパク質機能)を正確に記述
- 主要RCTは試験名・サンプルサイズ・主要評価項目の結果を具体的に引用
- 薬剤は一般名・作用機序・用量を明示
- 数値(OR, HR, p値, %, PFS/OS)は可能な限り具体的に
- 主要ガイドライン(NCCN, ESMO, JSCCR, USMSTF, ESGE等)との整合性に触れる
- 断定できない点・controversialな点は明確にそう述べる

【禁止事項】
- 曖昧な表現(「〜と言われている」等、主語不明の断定)
- 論文の捏造。提供された参考文献以外の具体的な試験名・著者名は挙げない
- 基礎的すぎる導入(「大腸癌は世界で〜」のような定型句)
- 箇条書きでの羅列(本文は連続した文章で、段落は2-4つ)

【出力形式】
必ず以下のJSONのみを返してください(コードフェンス・前置き禁止):
{
  "body": "講義本文。連続した日本語文章。900〜1100字厳守。段落は\\n\\nで区切る",
  "key_points": ["要点1(40字以内)", "要点2", "要点3", "要点4"]
}"""


def generate_lecture(plan: TopicPlan, refs: list[pubmed.Paper]) -> Lecture:
    refs_text = "\n".join(
        f"[{i+1}] {r.first_author_str} ({r.pub_date[:4]}) {r.title} — "
        f"{r.journal} (PMID {r.pmid})"
        for i, r in enumerate(refs)
    ) or "(参考文献なし。あなたの知識範囲で記述し、試験名・著者名の具体的記載は避けてください)"

    outline_text = "\n".join(f"- {o}" for o in plan.outline) or "(アウトラインなし)"

    user = (
        f"【本日の講義テーマ】\n{plan.topic_ja}\n\n"
        f"【選定理由】\n{plan.rationale}\n\n"
        f"【推奨アウトライン(必須ではない)】\n{outline_text}\n\n"
        f"【参考文献(本文中で (著者 et al., 誌名, 年) 形式で引用してよい)】\n{refs_text}\n\n"
        f"【目標文字数】{TARGET_CHARS}字(±{CHAR_TOLERANCE}字)\n\n"
        f"上記を踏まえ、専門医レベルの講義を生成してください。"
    )

    data = claude_client.call_json(
        system=LECTURE_SYSTEM, user=user,
        model=MODEL, max_tokens=3000, temperature=0.5,
    )

    body = data["body"].strip()
    char_count = len(body.replace("\n", ""))
    if abs(char_count - TARGET_CHARS) > CHAR_TOLERANCE:
        logger.warning(f"Body {char_count} chars (target {TARGET_CHARS}±{CHAR_TOLERANCE})")
    else:
        logger.info(f"Body {char_count} chars")

    return Lecture(
        plan=plan, body=body,
        key_points=data.get("key_points", []),
        references=refs,
    )


# -----------------------------------------------------------------------------
# Discord posting
# -----------------------------------------------------------------------------

def post_lecture(lecture: Lecture):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    notify.post(
        content=f"📖 **Colorectal Daily Lecture — {today}**\n"
                f"**テーマ: {lecture.plan.topic_ja}**\n"
                f"*領域: {lecture.plan.domain}  |  {lecture.plan.rationale}*"
    )

    notify.post_embed(
        title="📝 本文",
        description=lecture.body,
        color=0x1D3557,
    )

    if lecture.key_points:
        kp_text = "\n".join(f"▸ {kp}" for kp in lecture.key_points)
        notify.post_embed(
            title="🎯 Key Points",
            description=kp_text,
            color=0xE63946,
        )

    if lecture.references:
        ref_text = "\n".join(
            f"[{i+1}] {r.first_author_str} ({r.pub_date[:4]}) *{r.journal}*\n"
            f"     {r.title[:120]} — [PMID {r.pmid}]({r.url})"
            for i, r in enumerate(lecture.references)
        )
        notify.post_embed(
            title="📚 参考文献",
            description=ref_text,
            color=0x457B9D,
        )


# -----------------------------------------------------------------------------
# Artifact dump
# -----------------------------------------------------------------------------

def dump_lecture(lecture: Lecture):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    history.save_json_artifact(
        f"{ARTIFACT_DIR}/lecture_{date}.json",
        {
            "date": datetime.now(timezone.utc).isoformat(),
            "plan": asdict(lecture.plan),
            "body": lecture.body,
            "key_points": lecture.key_points,
            "references": [asdict(r) for r in lecture.references],
        },
    )

    md = [f"# {lecture.plan.topic_ja}\n"]
    md.append(f"**Date**: {date}  ")
    md.append(f"**Domain**: {lecture.plan.domain}  ")
    md.append(f"**Rationale**: {lecture.plan.rationale}\n")
    md.append("## 本文\n")
    md.append(lecture.body + "\n")
    md.append("## Key Points\n")
    for kp in lecture.key_points:
        md.append(f"- {kp}")
    md.append("\n## 参考文献\n")
    for i, r in enumerate(lecture.references, 1):
        md.append(f"{i}. {r.first_author_str} ({r.pub_date[:4]}). {r.title}. "
                  f"*{r.journal}*. [PMID {r.pmid}]({r.url})")
    history.save_artifact(f"{ARTIFACT_DIR}/lecture_{date}.md", "\n".join(md))


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main():
    # 1. Load history
    all_history = history.load_history(HISTORY_FILE)
    covered = history.filter_recent(all_history, HISTORY_DAYS)
    logger.info(f"Loaded {len(covered)} covered topics in last {HISTORY_DAYS} days")

    # 2. Topic selection
    plan = select_topic(covered)

    # 3. Fetch references
    refs = fetch_references(plan.topic_en_query, N_REFS)
    logger.info(f"Fetched {len(refs)} references")

    # 4. Generate lecture
    lecture = generate_lecture(plan, refs)

    # 5. Post & persist
    post_lecture(lecture)
    dump_lecture(lecture)

    # 6. Update history
    all_history.append({
        "date": datetime.now(timezone.utc).isoformat(),
        "topic_ja": plan.topic_ja,
        "domain": plan.domain,
        "rationale": plan.rationale,
    })
    all_history = history.trim_history(all_history, RETENTION_DAYS)
    history.save_history(HISTORY_FILE, all_history)
    logger.info("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("Fatal error")
        notify.post_error(e, "Colorectal Lecture Bot")
        sys.exit(1)
