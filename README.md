# Medical Bots — Colorectal Speciality Suite

大腸分野を中心とする消化器専門医向け学習支援bot群。共通モジュールを`shared/`にまとめたmonorepo構成。

## 収録Bot

| Bot | 種類 | 頻度 | 機能 |
|------|------|------|------|
| **Colorectal Weekly Digest** | 文献 | 週1 (月曜朝) | 大腸分野の1週間の論文を要約+トレンド俯瞰 |
| **Colorectal Daily Lecture** | 学習 | 毎日朝 | 大腸分野の専門医レベル1000字講義を配信 |
| **GI Quiz of the Day** | 試験対策 | 毎日(朝=問題/夜=解説) | 消化器専門医試験レベルの症例問題(大腸60%・全般40%) |

## ディレクトリ構成

```
medical-bots/
├── .github/workflows/
│   ├── colorectal-digest.yml     # 週次
│   ├── colorectal-lecture.yml    # 毎日
│   └── gi-quiz.yml               # 朝夜2回
├── shared/                       # 共通モジュール
│   ├── __init__.py
│   ├── pubmed.py                 # PubMed E-utilities wrapper
│   ├── claude_client.py          # Claude API + リトライ + JSON parsing
│   ├── notify.py                 # Discord Webhook投稿
│   ├── history.py                # 履歴・artifact管理
│   └── logging_config.py         # ロガー設定
├── bots/
│   ├── colorectal_digest/main.py
│   ├── colorectal_lecture/main.py
│   └── gi_quiz/main.py
├── artifacts/                    # 自動生成(bot別サブディレクトリ)
│   ├── colorectal_digest/
│   ├── colorectal_lecture/
│   └── gi_quiz/
├── requirements.txt
├── .gitignore
└── README.md
```

## セットアップ

### 1. リポジトリ作成とpush

```bash
gh repo create medical-bots --private
cd medical-bots
git add . && git commit -m "init" && git push
```

### 2. Secrets登録

Settings → Secrets and variables → Actions で以下を登録:

| Secret | 必須 | 説明 |
|--------|------|------|
| `ANTHROPIC_API_KEY` | ✅ | Anthropic Console で取得 |
| `DISCORD_WEBHOOK_COLORECTAL` | ✅ | digest / lecture 用チャンネルのWebhook |
| `DISCORD_WEBHOOK_QUIZ` | ✅ | quiz 用チャンネルのWebhook |
| `PUBMED_EMAIL` | ✅ | 任意のメールアドレス(NCBI識別用) |
| `PUBMED_API_KEY` | 任意 | NCBIアカウントで取得、レート制限緩和 |

Discord側は `#literature-digest`, `#daily-lecture`, `#quiz` のようにチャンネルを分け、それぞれのWebhookを登録する運用を推奨。lecture と digest を同じチャンネルに統合しても構わない。

### 3. Workflow権限

Settings → Actions → General → Workflow permissions を **"Read and write"** に変更(artifactsの自動commit用)。

### 4. 動作確認

各workflowの "Run workflow" ボタンで手動実行:

- **Colorectal Digest**: `days_back=3, max_papers=10` で短時間テスト
- **Colorectal Lecture**: パラメータ不要、即時実行
- **GI Quiz**: `mode=question` で問題投稿、後ほど `mode=answer` で解説投稿

## 実行スケジュール(JST)

| 時刻 | Bot | 内容 |
|------|------|------|
| 06:00 毎日 | Lecture | 大腸講義1000字 |
| 07:00 毎日 | Quiz | 朝の症例問題出題 |
| 07:00 火曜 | Digest | 先週1週間のCRC文献まとめ |
| 20:00 毎日 | Quiz | 前朝の問題の解説 |

朝通勤中に問題を考え、仕事帰りに解説で答え合わせ、という学習動線を想定。

## カスタマイズ

### Quiz の大腸偏重率を変更

`.github/workflows/gi-quiz.yml` の `COLORECTAL_WEIGHT` を変更。
- `1.0`: 100%大腸分野
- `0.5`: 半々
- `0.0`: 消化器全般のみ

### Lecture の文字数を変更

`TARGET_CHARS` 環境変数で調整。1500字の濃い内容にしたい場合:

```yaml
env:
  TARGET_CHARS: '1500'
```

### 実行時刻の調整

各workflowの `cron` を編集。GitHub Actionsは UTC なので JST+9h で計算する。例: JST 20:00 = UTC 11:00。

### Digest の対象領域を広げる

`bots/colorectal_digest/main.py` の `PUBMED_QUERY` を編集。例えば肛門疾患や便秘も含めたい場合、キーワードを追加する。

## コスト試算

Sonnet 4.6 ($3/$15 per MTok) 想定、1ヶ月あたり:

| Bot | 回数 | 1回コスト | 月額 |
|------|------|---------|------|
| Colorectal Digest (週1) | 4回 | ~$0.5 | $2.0 |
| Colorectal Lecture (毎日) | 30回 | ~$0.05 | $1.5 |
| GI Quiz (毎日、問題のみ生成は1回) | 30回 | ~$0.07 | $2.1 |
| **合計** | | | **~$5.6 (約870円)** |

IBD版の2botと合わせても月額1000〜1500円程度。

## トラブルシューティング

### Claude APIがoverloadedで失敗する
`shared/claude_client.py` のリトライ回数を増やすか、実行時刻を分散させる。

### 同じテーマが連続する
Lecture/Quizは `temperature=0.7-0.8` で多様性を確保しているが、履歴提示数を増やす(`covered[-60:]` を `[-90:]` に)と改善。

### 参考文献が0件
`topic_en_query` が具体的すぎる可能性。プロンプトで「クエリは5-10語で広めに」を強調。

### Discordで埋め込みが欠ける
1投稿4096字制限あり。`shared/notify.py` の `chunk_text` が自動分割するが、想定外の長文に遭遇した場合は要確認。

### GitHub Actionsの自動commitが失敗
- Workflow permissions が "Read and write" になっているか確認
- 複数workflowが同時にpushすると競合 → `concurrency` groupを設定済みだが、pull --rebaseで競合回避も実装

## 拡張アイデア

- **Drug Dosing Checker Bot** (slash command): 化学療法レジメンの用量計算
- **Consult Bot**: 症例要約をDiscordに投げると鑑別診断と次の検査を提案
- **Paper Writing Assistant**: 英文校正+引用文献提案
- **Guideline Diff Watcher**: 主要ガイドラインの更新検知
- **Bioinformatics Q&A Bot**: WGS/WES解析の技術質問対応

これらは `bots/` 配下に追加し、`shared/` モジュールを再利用する形で拡張可能。
