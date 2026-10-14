# 参照元・確認日

確認日：2026-09-22。以下は閲覧して設計基準へ反映した一次資料。一般のUI・グラフィック原則をサムネへ応用する部分は設計上の推論として扱う。

- ベース：サムネイル制作ワークフロー、文字とロゴの共通基準、人物保護・承認済みデザイン固定・制作後の改善記録。
- [yt-dlp公式ドキュメント](https://github.com/yt-dlp/yt-dlp)：flat-playlist、download=False、Python組込み、ページ付き一覧、要求間隔、メタデータ欠損を確認。公開サイトの変更で取得できなくなった場合は現行の公式案内を確認する。チャンネル収集はPythonライブラリとして使用し、動画本体を取得しない。
- [YouTube公式・カスタムサムネイル](https://support.google.com/youtube/answer/72431?hl=en)：確認時点の推奨解像度は通常動画3840×2160、Shorts2160×3840。通常動画16:9、Shorts9:16。アップロード容量上限は端末によって異なる。本スキルの1280×720・2MB未満は従来サムネッシーの互換納品プリセットであり、最新推奨値や全端末共通の上限という意味ではない。高解像度原本は別途保持する。

CTR改善率や「顔を入れると何%伸びる」といった未検証数値は共通ルールに採用していない。公開再生数の比較からCTRや因果を推定しない。

## 2026-09-22の追加調査

| ID | 一次資料 | 取り入れた点と適用の限界 |
|---|---|---|
| YT1 | [YouTube：サムネイルとタイトルのヒント](https://support.google.com/youtube/answer/12340300?hl=ja) | 読みやすさ、過密を避ける、視聴者と内容に沿う。確定文言を勝手に変える根拠にはしない |
| YT2 | [YouTube：タイトルとサムネイルのA/Bテスト](https://support.google.com/youtube/answer/16391400?hl=ja) | 最大3候補、視聴時間の割合で比較。初回3案の義務はユーザー指示による。勝者未確定の場合もある。自動投稿しない |
| YT3 | [YouTube：カスタムサムネイル](https://support.google.com/youtube/answer/72431?hl=ja) | 現行推奨と互換納品を区別。確認時の通常動画推奨3840×2160、16:9。2MBは全端末の共通上限ではない |
| NN1 | [NN/g：5 Principles of Visual Design](https://www.nngroup.com/articles/principles-visual-design/) | 相対サイズ、情報の優先順位、バランス、コントラスト、まとまり。日本語の文字サイズ種類を一律制限しない |
| NN2 | [NN/g：Proximity Principle](https://www.nngroup.com/articles/gestalt-proximity/) | 近接で関係を示す。ロゴと説明、数字と単位の所属を誤読させない設計へ応用 |
| NN3 | [NN/g：Common Region](https://www.nngroup.com/articles/common-region/) | 同じ囲いが情報をまとめる。帯や枠を増やしすぎず意味ごとに使う |
| AD1 | [Adobe：Layout basics](https://www.adobe.com/learn/illustrator/web/layout-basics) | 整列、反復、コントラスト、階層。同じ背景画像の複製でなくルールの一貫性へ応用 |
| AD2 | [Adobe：Layer effects in Photoshop on the web](https://www.adobe.com/learn/photoshop/web/layer-effects-photoshop-web) | グラデーションの角度・スケール、Stroke、Drop Shadowを別々に制御。全レイヤーへ装飾する規則ではない |
| MO1 | [モリサワ：第一回 新ゴ（上）](https://www.morisawa.co.jp/culture/inside-story/1996) | 書体ファミリーとウェイトの役割。同じ系統でも太さごとの字形を尊重。主チャンネルで新ゴ使用と断定する資料ではない |
| AD3 | [Adobe：Common graphic design mistakes](https://www.adobe.com/express/learn/blog/common-graphic-design-mistakes) | 意味→構造→スタイルの順、縮小検査、過密・不統一の修正。記事の推奨フォント数や削減率は今回のPSDより優先しない |

仕様やテスト機能を実利用する時は公式情報を再確認する。公開記事の一般的な数値をこのチャンネルの効果測定に転用しない。
