# 粗密二段階 伏線サーチ V2

終盤の真相（Payoff）から過去へ遡り、粗い場面検索と候補区間の詳細再解析を行う独立Streamlitデモです。既存の`foreshadowing-video-search`には変更を加えていません。

## 起動

```bash
cd /Users/kaede/Documents/Codex/2026-08-18/new-chat/foreshadowing-video-search-v2
./start_app.command
```

ブラウザが自動で開かない場合は、ターミナルに表示された`http://localhost:...`を開きます。

## 10段階

1. 動画アップロード
2. 6〜10秒間隔の代表フレーム抽出
3. 粗い場面インデックス作成
4. 終盤の真相入力
5. 人物・物体・手段・必要条件への構造化
6. Embeddingによる粗検索
7. 上位候補の前後を0.5〜1秒間隔で再抽出
8. 映像的証拠の詳細確認
9. 因果関係・再解釈度・F–T–Pによる再ランキング
10. 伏線候補・時刻・映像・根拠・費用表示

## 研究記録の保存

解析後に「実験結果をZIPで保存」を押すと、次を一括保存します。

- `run_summary.json`：真相構造、全結果、API利用量、処理時間
- `coarse_results.csv`：粗検索順位と最も近かった検索文
- `detailed_candidates.json`：詳細証拠、F–T–P、スコア
- `settings.json`：再現に必要な解析設定
- `report.html`：人が読みやすい結果レポート
- `evidence_frames/`：候補区間の証拠画像

元動画はZIPへ複製せず、動画名・サイズ・SHA-256を記録します。

伏線検索の終了時刻は必須です。TriggerまたはPayoffが始まる直前を指定し、それ以後の場面を過去候補へ混入させないでください。

## APIキー

画面へ入力するか、起動前に環境変数を設定します。キーをソースコードへ書かないでください。

```bash
export GEMINI_API_KEY="あなたのAPIキー"
```

## 研究デモとしての注意

- 出力は作者の意図を断定する正解ではなく、モデルが提示する伏線候補です。
- 最初は権利上問題のない30秒〜3分の動画で試してください。
- F–T–PのTriggerは、入力や候補映像から確認できない場合は未確認になります。
- 表示料金はAPI応答のトークン数と登録単価から算出した概算です。実請求はGoogle側を確認してください。
- アプリ内の単価は2026年9月時点のGemini Developer API Standard料金です。料金改定時には`core.py`の`MODEL_PRICES_USD`を更新してください。
