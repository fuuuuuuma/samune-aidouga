# Flare / Sunburst の自動選択

2026-09-22確認。モデル選択と、案数・見た目の合否は別管理する。初回3案／修正2案、全参考の目視、主チャンネル優先、過去の修正条件は変えない。単にモデル名をプロンプトに書いて「切り替えた」と報告しない。

## 接続の選択

一般配布時はCodex内蔵画像生成を標準とする。内蔵ツールにモデル指定欄がない場合、Flare/Sunburstの実選択はできない。ユーザーがOpenAI APIを選び、`DATA_ROOT/production-policy.json` の `model_routing.backend` が `openai_api`、`api_use_authorized` がtrueの場合に、同梱の維持管理するAPIアダプターを使う。

APIキーは承認されたenvファイルまたは環境変数 `OPENAI_API_KEY` から読む。スキル、プロンプト、バッチ、公開リポジトリにキーを記載しない。キー取得はOpenAI Developersの安全な設定手順に従う。接続失敗は未接続として報告し、内蔵ツールや別サービスへ黙って切り替えない。APIは従量課金。ユーザーが依頼した生成範囲に限り実行する。

## AIが判断し、プログラムが条件を検査する

ラフと支給素材を目視したAIが、各案の `routing-job.json` の `visual_evidence` と制約を記入する。コードは画像を見たことにしない。文字固定・添付画像のrole・修正フェーズ・保持指定からも強制的に条件を補う。

| 条件 | 選択 | 根拠の種類 |
|---|---|---|
| 人物本人性、ラフの構図、正式ロゴ、確定文字、変更指定外の保持 | Sunburst | 公式の編集精度重視を、この制作要件へ適用した運用判断 |
| 保持制約のない新規背景・自由なビジュアル探索 | Flare | 公式の高速な日常生成向けという位置付け |
| Flareで文字・本人・ロゴ・配置・保持が実際に不合格 | Sunburstで限定修正 | QCに基づく運用判断 |
| Sunburstで手だけが不合格、局所マスクと元の人物参照がある | Flare Highで局所比較 | 紹介記事の一事例を限定的な試行条件として採用 |
| ユーザーがモデルを明示 | 指定モデル | user_quoteとevidenceを記録 |

初稿だから必ずFlare、修正だから必ず高品質化、とは決めない。同じ保持条件なら3案ともSunburstでよい。3案の違いは構図・文字階層・許された表現で設計する。Low/Medium/High等はモデル名と独立した品質設定で、標準はHigh。Maxへ無条件に上げない。Flareの方が1枚あたり安いという保証はない。

## 実行

1. 通常の `request_contract.json`、全件参考確認、全案の完全版JSONと `batch.json` を先に完成させる。
2. `batch.backend` を `openai_api` にする。各案の `routing_job` を設定。`assets/image-routing-job.json` を `initial/A/routing-job.json` 等へ複製する。ファイル内のパスはこのjobからの相対パス。batch内のパスはbatchからの相対パス。
3. 実際に送る画像を `images` に記録する。各項目は `path`、`sha256`、`role`、`description`。roleはrough / identity / logo / edit_target / style / background / other。ラフ、正式ロゴ、本人素材を忘れず添付する。PSDはプレビューPNG等を用意し、実際にアップロードする画像をcontractへ登録、PSD元情報は設計根拠として別途残す。
4. 修正時はユーザー採用画像を `edit_target` として先頭に添付。本人参照は常に支給された元写真。参照一覧を文章で書くだけで添付した扱いにしない。
5. オフラインで各案をplanし、条件と選択理由を確認する。実行時にもバッチ全体の案数・固定条件を再検査する。

```bash
SKILL_ROOT/.venv/bin/python SKILL_ROOT/scripts/image_model_router.py plan --job PROJECT/initial/A/routing-job.json
SKILL_ROOT/.venv/bin/python SKILL_ROOT/scripts/image_model_router.py check-connection --env-file CONFIRMED_ENV
SKILL_ROOT/.venv/bin/python SKILL_ROOT/scripts/image_model_router.py run \
  --job PROJECT/initial/A/routing-job.json \
  --policy DATA_ROOT/production-policy.json --env-file CONFIRMED_ENV
```

実際には絶対パスへ置換する。`check-connection` はモデル情報の取得だけで画像は生成しない。モデルが見えることと、画像生成権限・課金枠・組織検証の完了は同じではない。

各runは画像1枚だけ。画像添付があればImage APIのedits、なければgenerationsを呼ぶ。`model` 引数で実モデルを指定する。SDKの自動再試行は0。生成画像・リクエストID・指定モデル・選択理由・入出力ハッシュ・利用量（返却された場合）を `api-runs/<案ID>/attempt-01/` へ記録する。モデル名がレスポンスに無ければ `reported_model: null` とし、指定モデルと応答で確認できたモデルを混同しない。

生成後、batchの各案の `original` を保存原本へ、`model_receipt` をreceiptへ設定する。全案を実画像QC・互換JPEG化して通常のdelivery検査を通す。APIの成功は見た目の合格ではない。

## 一度だけのQC再試行

第一生成とQCに基づく限定再試行の最大2リクエスト／案。2回目で未解決なら元素材を保つ構成や別実写真へ戻る方針を報告する。名前を変えた新バッチで無断リトライを継続しない。案数を削って帳尻を合わせない。

再試行は `attempt: 2`、先行 `previous_receipt` と `previous_qc` を指定し、先行出力を先頭のedit_targetにする。QC JSONは実際の画像の `image_sha256`、`checked_at`、`note`、`failures`（identity / hands / text / logo / layout / preservation / finish）を持つ。

手の局所切替のみ `isolated_hand_fix: true` とし、元人物参照、`protected_region_note`、`mask: {path, sha256}` を必須にする。マスクは先頭画像と同寸PNG、透明部分が編集対象、非透明部分が保持対象。対象外に顔や文字が入っていないことを目視する。マスクは編集ガイドであり、画素の完全保持を保証しない。全体を再描画して顔を直したつもりにしない。

`requesting` / `failed_or_unknown` / `response_invalid` が残る場合、自動再送しない。タイムアウトしていてもサーバー側で処理・課金された可能性がある。保存原本とリクエストIDを確認し、必要ならユーザーへ事情を伝える。APIエラー本文や認証情報をログに出さない。

## 出典と解釈の限界

- [OpenAI Image generation guide](https://developers.openai.com/api/docs/guides/image-generation)：Sunburstは編集精度、Flareは高速な高品質生成という公式位置付け。画像編集・マスク・出力形式。
- [Sunburst](https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst) / [Flare](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare)：モデルID、品質設定、エンドポイント。実際のアカウント利用可否は別途確認が必要。
- [よいち氏の人物素材検証](https://note.com/keen_quoll5265/n/na264352a0eb4)：一つの非公開クライアント素材で、本人性と手の修正を別工程として試した経験。手の得意不得意やモデルの普遍的な優劣を証明する比較試験ではない。全文を複製せず、この限定的な示唆のみ運用へ反映した。
