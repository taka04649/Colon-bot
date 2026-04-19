"""
GI Quiz Bot
- 消化器専門医試験・J-OSLERレベルの症例ベース問題を毎日配信
- 大腸分野を中心に消化器全般をカバー
- 実行モード:
    * "question": 新しい問題を生成して投稿(問題+正解+解説を同時生成し保存)
    * "answer":   前日出題した問題の解説を投稿
- GitHub Actionsで朝に question、夜に answer を実行する2-step構成
"""

from __future__ import annotations

import os
import sys
import json
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared import claude_client, notify, history  # noqa: E402
from shared.logging_config import setup_logging  # noqa: E402

logger = setup_logging("gi-quiz")

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "90"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "360"))
# 大腸偏重率(0-1)。0.6なら60%の確率で大腸分野、40%で消化器全般
COLORECTAL_WEIGHT = float(os.environ.get("COLORECTAL_WEIGHT", "0.6"))

ARTIFACT_DIR = str(ROOT / "artifacts" / "gi_quiz")
HISTORY_FILE = f"{ARTIFACT_DIR}/covered_topics.json"
PENDING_FILE = f"{ARTIFACT_DIR}/pending_question.json"

# 出題ドメイン(大腸)
COLORECTAL_DOMAINS = [
    "CRC分子生物学とコンセンサス分類",
    "MSI-H CRCと免疫療法",
    "転移性CRCの治療選択(RAS, BRAF, HER2)",
    "直腸癌TNT・watch-and-wait戦略",
    "Stage III adjuvant(IDEA)",
    "ctDNA MRDに基づくadjuvant判断",
    "CRC screening戦略",
    "colonoscopy品質指標・interval cancer",
    "ポリープ切除適応と深達度診断",
    "JNET/NICE/Kudo pit pattern",
    "SSL・serrated pathway",
    "ESDの適応・合併症管理",
    "Cold vs hot snare polypectomy",
    "Lynch症候群の診断と管理",
    "FAP・MUTYH関連ポリポーシス",
    "CRC surveillance間隔",
    "直腸癌TME・側方郭清適応",
    "術後合併症(LARS, 縫合不全)",
    "大腸憩室炎",
    "IBS診断治療",
    "便秘の病態分類",
    "CDIの診断治療",
    "IBD関連CRC surveillance",
]

# 出題ドメイン(消化器全般)
GENERAL_GI_DOMAINS = [
    "肝炎ウイルス治療(HCV DAA, HBV NA)",
    "NAFLD/MASLD・MASH",
    "肝硬変合併症(腹水, SBP, HE)",
    "HCC治療選択(BCLC)",
    "胆管癌・膵癌診断治療",
    "急性膵炎の重症度評価と治療",
    "慢性膵炎・自己免疫性膵炎(AIP)",
    "胆石症・胆嚢炎・胆管炎(東京ガイドライン)",
    "NETの診断と治療",
    "GISTの診断と分子標的治療",
    "GERD・Barrett食道",
    "食道癌(扁平上皮癌・腺癌)",
    "胃癌治療(早期・進行)",
    "H. pylori診断治療",
    "機能性ディスペプシア",
    "好酸球性食道炎(EoE)",
    "セリアック病",
    "自己免疫性肝炎・PBC・PSC",
    "小腸疾患(lymphangiectasia, celiac等)",
    "消化管出血の診断と治療",
    "IBD診断治療(UC, CD)",
    "腸閉塞・イレウス",
    "急性虫垂炎",
]


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------

@dataclass
class QuizContent:
    domain: str
    topic: str
    scenario: str            # 症例プレゼンテーション
    question: str            # 設問
    choices: list[str]       # 選択肢(5つ、A〜E)
    correct_letter: str      # "A" - "E"
    correct_index: int       # 0-4
    rationale_correct: str   # 正解選択肢の解説
    rationale_others: list[str]  # 不正解選択肢それぞれの解説
    teaching_points: list[str]   # Take-home messages
    references: list[str]    # 引用ガイドライン・文献


# -----------------------------------------------------------------------------
# Prompts
# -----------------------------------------------------------------------------

QUIZ_SYSTEM = """あなたは日本の消化器病学会専門医試験・J-OSLER考察作成レベルの
症例ベース問題を作成する消化器内科シニアスタッフです。

【作問要件】
- A型問題(single best answer)、5択で必ず作成
- 症例は臨床的にリアリスティックで、年齢・性別・主訴・既往・身体所見・検査値・画像を含む
- 不正解選択肢(distractor)は鑑別として合理的なものを配置し、単なる埋草にしない
- 解説は機序・エビデンス・ガイドラインを引用して記述
- 引用する主要試験名・ガイドライン(NCCN, ESMO, JSCCR, ECCO, AGA, USMSTF, 東京ガイドライン等)は
  実在するもののみ。具体的な数値・試験名で不確かなものは記載しない
- 対象は消化器内科専門医レベル。基礎的すぎる問題は作らない
- 過去にカバーしたトピックは避ける

【出力形式】
必ず以下のJSONのみを返してください(コードフェンス・前置き禁止):
{
  "domain": "出題ドメイン",
  "topic": "本問のトピック(30字以内)",
  "scenario": "症例プレゼンテーション(200-400字の日本語)",
  "question": "設問(「最も適切な○○はどれか」等)",
  "choices": ["選択肢A", "選択肢B", "選択肢C", "選択肢D", "選択肢E"],
  "correct_letter": "A"から"E"のいずれか,
  "rationale_correct": "正解選択肢が正しい理由(200-300字、機序+エビデンス)",
  "rationale_others": ["選択肢Aの解説(正解含む、各60-120字)", ...5つ],
  "teaching_points": ["要点1(40字以内)", "要点2", "要点3"],
  "references": ["引用ガイドライン・試験名1", "引用2"]
}"""


def select_and_generate_quiz(covered: list[dict]) -> QuizContent:
    """テーマ選択と問題生成を1回のAPI呼び出しで実施"""
    # 大腸/全般どちらのドメイン群から出題するか決定
    use_colorectal = random.random() < COLORECTAL_WEIGHT
    domain_pool = COLORECTAL_DOMAINS if use_colorectal else GENERAL_GI_DOMAINS
    pool_label = "大腸分野" if use_colorectal else "消化器全般"

    covered_text = "\n".join(
        f"- [{e.get('date', '?')[:10]}] {e.get('domain', '?')}: {e.get('topic', '?')}"
        for e in covered[-60:]
    ) or "(履歴なし)"

    domains_text = "\n".join(f"- {d}" for d in domain_pool)

    user = (
        f"【本日の出題カテゴリ】{pool_label}\n\n"
        f"【候補ドメイン】\n{domains_text}\n\n"
        f"【過去{HISTORY_DAYS}日の出題履歴(重複回避のため参照)】\n{covered_text}\n\n"
        f"上記カテゴリから重複しないトピックを選び、専門医試験レベルの症例問題を1問作成してください。"
    )

    data = claude_client.call_json(
        system=QUIZ_SYSTEM, user=user,
        model=MODEL, max_tokens=3500, temperature=0.7,
    )

    correct_letter = data["correct_letter"].strip().upper()
    correct_index = ord(correct_letter) - ord("A")

    return QuizContent(
        domain=data.get("domain", "未分類"),
        topic=data.get("topic", ""),
        scenario=data["scenario"],
        question=data["question"],
        choices=data["choices"],
        correct_letter=correct_letter,
        correct_index=correct_index,
        rationale_correct=data["rationale_correct"],
        rationale_others=data.get("rationale_others", []),
        teaching_points=data.get("teaching_points", []),
        references=data.get("references", []),
    )


# -----------------------------------------------------------------------------
# Discord posting - question
# -----------------------------------------------------------------------------

def post_question(quiz: QuizContent, date_label: str):
    notify.post(
        content=f"📝 **GI Quiz of the Day — {date_label}**\n"
                f"**Topic**: {quiz.topic}  |  **Domain**: {quiz.domain}\n"
                f"解説は後ほど配信されます。"
    )

    notify.post_embed(
        title="🏥 症例",
        description=quiz.scenario,
        color=0x2A9D8F,
    )

    choices_text = "\n".join(
        f"**{chr(ord('A') + i)}.** {c}"
        for i, c in enumerate(quiz.choices)
    )
    notify.post_embed(
        title=f"❓ 設問",
        description=f"{quiz.question}\n\n{choices_text}\n\n"
                    f"*自分の答えを考えてから、解説配信をお待ちください。*",
        color=0xE9C46A,
    )


# -----------------------------------------------------------------------------
# Discord posting - answer
# -----------------------------------------------------------------------------

def post_answer(quiz: QuizContent, date_label: str):
    notify.post(
        content=f"💡 **解説 — {date_label}**\n"
                f"**Topic**: {quiz.topic}"
    )

    # 症例の再掲(解説だけ読む人向けに短く)
    scenario_recap = quiz.scenario[:400] + ("..." if len(quiz.scenario) > 400 else "")
    notify.post_embed(
        title="📋 症例(再掲)",
        description=scenario_recap,
        color=0x6C757D,
    )

    # 各選択肢の解説
    choices_review = []
    for i, (choice, rationale) in enumerate(zip(quiz.choices, quiz.rationale_others)):
        letter = chr(ord("A") + i)
        mark = "✅" if i == quiz.correct_index else "❌"
        choices_review.append(f"{mark} **{letter}. {choice}**\n{rationale}")
    notify.post_embed(
        title="📝 各選択肢の検討",
        description="\n\n".join(choices_review),
        color=0x457B9D,
    )

    # 正解の詳細解説
    notify.post_embed(
        title=f"✅ 正解: {quiz.correct_letter}. {quiz.choices[quiz.correct_index]}",
        description=quiz.rationale_correct,
        color=0x06A77D,
    )

    # Teaching points
    if quiz.teaching_points:
        tp_text = "\n".join(f"▸ {tp}" for tp in quiz.teaching_points)
        notify.post_embed(
            title="🎯 Teaching Points",
            description=tp_text,
            color=0xE63946,
        )

    # References
    if quiz.references:
        ref_text = "\n".join(f"• {r}" for r in quiz.references)
        notify.post_embed(
            title="📚 参考",
            description=ref_text,
            color=0x6C757D,
        )


# -----------------------------------------------------------------------------
# Artifact
# -----------------------------------------------------------------------------

def save_quiz_artifact(quiz: QuizContent, date: str):
    history.save_json_artifact(
        f"{ARTIFACT_DIR}/quiz_{date}.json",
        {"date": date, **asdict(quiz)},
    )

    md = [f"# {quiz.topic}\n"]
    md.append(f"**Date**: {date}  ")
    md.append(f"**Domain**: {quiz.domain}\n")
    md.append("## 症例\n")
    md.append(quiz.scenario + "\n")
    md.append(f"## 設問\n\n{quiz.question}\n")
    for i, c in enumerate(quiz.choices):
        letter = chr(ord("A") + i)
        md.append(f"- **{letter}.** {c}")
    md.append(f"\n## 正解\n\n**{quiz.correct_letter}. {quiz.choices[quiz.correct_index]}**\n")
    md.append("### 解説\n")
    md.append(quiz.rationale_correct + "\n")
    md.append("### 各選択肢\n")
    for i, (c, r) in enumerate(zip(quiz.choices, quiz.rationale_others)):
        letter = chr(ord("A") + i)
        mark = "✅" if i == quiz.correct_index else "❌"
        md.append(f"- {mark} **{letter}.** {c}  \n  {r}\n")
    md.append("## Teaching Points\n")
    for tp in quiz.teaching_points:
        md.append(f"- {tp}")
    md.append("\n## References\n")
    for r in quiz.references:
        md.append(f"- {r}")
    history.save_artifact(f"{ARTIFACT_DIR}/quiz_{date}.md", "\n".join(md))


# -----------------------------------------------------------------------------
# Pending question persistence
# -----------------------------------------------------------------------------

def save_pending(quiz: QuizContent, date: str):
    """問題投稿時に、翌日の解説用にpendingファイルへ保存"""
    history.save_json_artifact(PENDING_FILE, {"date": date, **asdict(quiz)})


def load_pending() -> Optional[tuple[QuizContent, str]]:
    """pendingから問題を読み込む"""
    if not os.path.exists(PENDING_FILE):
        return None
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        date = data.pop("date")
        quiz = QuizContent(**data)
        return quiz, date
    except Exception as e:
        logger.error(f"Failed to load pending: {e}")
        return None


def clear_pending():
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def run_question_mode():
    """問題生成・投稿モード"""
    all_history = history.load_history(HISTORY_FILE)
    covered = history.filter_recent(all_history, HISTORY_DAYS)
    logger.info(f"Loaded {len(covered)} covered quizzes in last {HISTORY_DAYS} days")

    quiz = select_and_generate_quiz(covered)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    post_question(quiz, date)
    save_pending(quiz, date)

    # 履歴更新
    all_history.append({
        "date": datetime.now(timezone.utc).isoformat(),
        "domain": quiz.domain,
        "topic": quiz.topic,
    })
    all_history = history.trim_history(all_history, RETENTION_DAYS)
    history.save_history(HISTORY_FILE, all_history)

    logger.info(f"Question posted: {quiz.topic}")


def run_answer_mode():
    """解説投稿モード"""
    loaded = load_pending()
    if not loaded:
        logger.warning("No pending question found. Skipping.")
        notify.post(content="⚠️ GI Quiz: 解説対象の問題が見つかりませんでした。")
        return

    quiz, date = loaded
    post_answer(quiz, date)
    save_quiz_artifact(quiz, date)
    clear_pending()

    logger.info(f"Answer posted: {quiz.topic}")


def main():
    mode = os.environ.get("QUIZ_MODE", "question").lower()
    logger.info(f"Running in {mode} mode")
    if mode == "question":
        run_question_mode()
    elif mode == "answer":
        run_answer_mode()
    else:
        raise ValueError(f"Unknown QUIZ_MODE: {mode} (expected 'question' or 'answer')")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("Fatal error")
        notify.post_error(e, "GI Quiz Bot")
        sys.exit(1)
