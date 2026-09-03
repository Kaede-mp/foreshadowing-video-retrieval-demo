# Coarse-to-Fine Foreshadowing Retrieval with Narrative Causality

物語終盤で判明した真相から過去の映像へ遡り、伏線候補を時刻・映像証拠・理由とともに検索するStreamlit研究デモです。

日本語名：**物語因果関係を用いた粗密二段階伏線検索**

> 本アプリが提示するのは、映像と字幕から推定した「伏線候補」です。作者が意図した伏線であることを断定するものではありません。

## 研究目的

一定間隔のフレーム抽出は、短時間だけ映る小道具・視線・手の動きなどを見落とす場合があります。一方、動画全体を最初から高密度で解析すると、処理時間とAPI費用が増加します。

本デモでは次の粗密二段階方式を試します。

```text
動画
  ↓
粗い場面インデックスを作成
  ↓
終盤の真相を人物・物体・手段・必要条件へ構造化
  ↓
複数の検索文で過去場面を広く検索
  ↓
上位候補の前後だけを0.5〜1秒間隔で再解析
  ↓
因果関係・再解釈度・映像証拠で再ランキング
  ↓
伏線候補・時刻・根拠・F–T–P・API費用を表示
```

## 主な機能

- 動画から6〜10秒間隔で代表フレームを抽出
- Geminiによる事実中心の場面記録作成
- 終盤の真相を人物・行動・手段・証拠・必要条件へ構造化
- Gemini Embeddingによる複数クエリ検索
- 上位候補区間だけを高密度で再解析
- Physical / Motivational / Psychological / Enablingの物語因果関係を評価
- 真相を知る前後の意味変化を「再解釈度」として評価
- Foreshadow–Trigger–Payoff（F–T–P）形式で結果を説明
- Trigger／Payoffが伏線より上位になることを防ぐ役割補正
- APIトークン、概算費用、処理時間を表示
- 研究記録をJSON・CSV・HTML・証拠画像入りZIPとして保存

## 必要なもの

- Python 3.11以上
- Gemini APIキー
- mp4 / movなどの動画
- 任意でSRT字幕

Gemini APIキーは[Google AI Studio](https://aistudio.google.com/apikey)で取得できます。

## インストールと起動

### macOS：簡単な起動方法

ターミナルでリポジトリへ移動し、次を実行します。

```bash
./start_app.command
```

初回は仮想環境の作成と依存パッケージのインストールが行われます。

### 手動で起動する場合

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

起動後、ターミナルに表示される`http://localhost:8501`をブラウザで開きます。

## APIキーの扱い

起動後の画面へAPIキーを入力できます。環境変数を利用する場合は次のように設定します。

```bash
export GEMINI_API_KEY="あなたのAPIキー"
streamlit run app.py
```

APIキーはソースコードへ記入しないでください。`.env`と`.streamlit/secrets.toml`は`.gitignore`で除外しています。

## 付属サンプルで試す

`sample_data/`に、研究デモ用に制作した84秒・19ショットのオリジナル短編ミステリーを収録しています。

```text
sample_data/
├── mystery_blue_ink_experiment_v3.mp4
├── mystery_blue_ink_experiment_v3.srt
└── ground_truth_v3.json
```

アプリでは次のように入力します。

| 項目 | 設定値 |
|---|---|
| 動画 | `sample_data/mystery_blue_ink_experiment_v3.mp4` |
| 字幕 | `sample_data/mystery_blue_ink_experiment_v3.srt` |
| 伏線検索の終了時刻 | `00:48` |
| 粗抽出の間隔 | `6秒` |
| 粗検索の候補数 | `8件` |
| 詳細解析する候補数 | 初回検証では`8件` |
| 候補の前後 | `4秒` |
| 詳細抽出の間隔 | `0.5秒` |

「終盤で判明した真相（Payoff）」には次を入力します。

```text
田中は机の引き出しにあった予備鍵を使って研究室へ侵入しており、右手の指に付着した青い特殊インクがその証拠だった。
```

このサンプルでは、青い指の視覚的伏線を`00:21〜00:23`の短時間だけ提示しています。6秒間隔の粗抽出では見落とす可能性がありますが、`00:18`付近が候補となり、その前後を詳細再解析すれば発見できるよう設計しています。

正解時刻とF–T–Pは`sample_data/ground_truth_v3.json`に記録しています。

## スコア

候補の基本スコアは次の5要素から計算します。

```text
0.25 × 意味類似度
+ 0.25 × 物語因果関係
+ 0.20 × 真相による再解釈度
+ 0.15 × 人物・物体の一致
+ 0.15 × 映像・字幕の証拠強度
```

その後、モデルが判定した物語上の役割で補正します。

| 判定 | 補正 |
|---|---:|
| 伏線候補 | ×1.0 |
| ミスリード候補 | ×0.6 |
| 単なる関連 | ×0.2 |
| 根拠不足 | ×0.1 |
| Trigger / Payoff | ×0.0 |

重みは現時点の暫定値です。今後、検証データを用いた調整とアブレーション実験を行う必要があります。

## 実験結果の保存

解析後に「📦 実験結果をZIPで保存」を押すと、次の研究記録を一括保存できます。

```text
run_summary.json
coarse_results.csv
detailed_candidates.json
settings.json
report.html
evidence_frames/
```

- `run_summary.json`：真相構造、検索結果、API利用量、処理時間
- `coarse_results.csv`：粗検索順位と最も近かった検索文
- `detailed_candidates.json`：詳細証拠、F–T–P、スコア
- `settings.json`：再現に必要な解析設定
- `report.html`：人が読みやすい実験レポート
- `evidence_frames/`：詳細解析した候補区間のフレーム

元動画はZIPへ複製せず、動画名・ファイルサイズ・SHA-256を記録します。

## 研究背景

本デモは、以下の研究から着想を得て、それぞれを伏線動画検索へ応用しています。

- [iRAG: Advancing RAG for Videos with an Incremental Approach](https://arxiv.org/abs/2404.12309)：関連区間を問い合わせ後に詳細化する増分的処理
- [DrVideo: Document Retrieval Based Long Video Understanding](https://openaccess.thecvf.com/content/CVPR2025/html/Ma_DrVideo_Document_Retrieval_Based_Long_Video_Understanding_CVPR_2025_paper.html)：動画文書を検索し、元映像へ戻って不足情報を確認する処理
- [Inferring Narrative Causality between Event Pairs in Films](https://aclanthology.org/W17-5540/)：4種類の物語因果関係
- [Neural representations of naturalistic events are updated as our understanding of the past changes](https://elifesciences.org/articles/79045)：結末による過去場面の遡及的再解釈
- [Codified Foreshadowing-Payoff Text Generation](https://arxiv.org/abs/2601.07033)：Foreshadow–Trigger–Payoff構造

これらの原研究が本デモと同じ伏線動画検索システムを提案しているわけではありません。各要素を動画の遡及的伏線検索へ統合する部分が本デモの提案です。

## 現在の制約

- 真相と検索終了時刻はユーザーが入力します。
- 人物同一性や作者の意図を完全には判定できません。
- 間隔の短い伏線が粗検索候補に入らない場合、詳細解析まで到達しません。
- LLMが映像にない因果関係を推測する可能性があります。
- API費用表示は概算であり、実際の請求はGoogle側を確認する必要があります。
- 付属サンプルはイラストを切り替える研究用動画で、実写ドラマとは性質が異なります。

## テスト

```bash
python -m unittest test_core.py
```

## 今後の予定

- 固定フレーム方式との比較
- Agentic Video Understanding方式との比較
- 検索クエリごとの候補多様性を保証する選択方法
- 正解ラベルを用いたPrecision@K・Recall@K・時間IoUの評価
- 実写・複数種類の伏線を含む評価データセット
