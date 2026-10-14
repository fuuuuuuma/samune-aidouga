# sumnessy-team — Codex Plugin

ラフ画像、支給素材、チャンネル別の参考資料をもとに、YouTubeサムネイルの構成・制作・修正・QCを支援するCodexプラグインです。初回はA/B/Cの3案、修正時はR1/R2の2案を独立画像として作るワークフローを提供します。

## Install

```bash
codex plugin marketplace add fuuuuuuma/sumnessy-team
codex plugin add sumnessy-team@sumnessy-team
```

インストール後、新しいCodexチャットで `$sumnessy-team` と依頼内容、ラフ、支給素材を指定してください。導入状況は `codex plugin marketplace list` と `codex plugin list` で確認できます。

## 必要な環境

- Codex CLIまたはCodexアプリのプラグイン機能
- Python 3.10以降
- チャンネル情報の収集には `yt-dlp` とネット接続
- 画像の収集・合成など一部スクリプトには `requirements.txt` の依存関係
- 任意のOpenAI APIモデル経路には、利用者が設定した `OPENAI_API_KEY` とAPI利用料が必要です。既定はCodex内蔵の画像生成機能です。

## データとプライバシー

チャンネル画像、人物素材、修正履歴、背景ライブラリなどの作業データは、プラグインの外にある利用者の `DATA_ROOT` に保存します。このリポジトリには作業データ、画像素材、APIキーを含めません。プラグインはYouTubeやXへ自動投稿しません。

## ライセンス

MIT License。詳細は [LICENSE](LICENSE) を参照してください。
