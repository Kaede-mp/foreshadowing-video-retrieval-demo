import csv
import hashlib
import html
import io
import json
import os
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pandas as pd
import streamlit as st

from core import (
    MODEL_PRICES_USD,
    calculate_cost,
    clamp_score,
    combine_scores,
    cosine_scores,
    estimate_run,
    extract_json,
    format_time,
    merge_usage,
    parse_srt,
    parse_timecode,
    role_adjusted_score,
    subtitles_for_window,
)

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover
    genai = None
    types = None


EMBEDDING_MODEL = "gemini-embedding-2"
APP_VERSION = "2.1-research-export"

COARSE_PROMPT = """
あなたは映像資料の記録係です。与えられた代表フレームと同時刻の字幕だけを根拠に、粗検索用の場面記録を作ってください。
作者の意図、犯人、伏線を推測せず、見える事実と不確かな解釈を区別してください。次のJSONだけを返してください。
{
  "visual_facts": ["映像から確認できる事実"],
  "characters": ["人物。名前不明なら外見上の呼称"],
  "objects": ["見える物体"],
  "actions": ["確認できる行動"],
  "dialogue_facts": ["字幕上の発言"],
  "possible_contradictions": ["映像と発言の食い違い。なければ空配列"],
  "uncertainties": ["代表フレームだけでは断定できない点"],
  "search_text": "検索用の日本語2〜4文"
}
""".strip()

TRUTH_PROMPT = """
物語終盤で判明した真相を、過去場面の検索に利用できる形へ分解してください。入力にない事実は追加しないでください。
Foreshadowは過去の手掛かり、Triggerは手掛かりの意味に気づかせる情報、Payoffは伏線が回収され真相が確定する出来事です。
次のJSONだけを返してください。
{
  "payoff": {"summary": "真相", "people": [], "actions": [], "methods": [], "objects": [], "evidence": []},
  "required_prior_conditions": ["真相を可能にした過去の条件"],
  "possible_triggers": ["伏線と真相を結び付ける情報として探す内容"],
  "contradictions_to_seek": ["過去に探す発言・行動の矛盾"],
  "retrieval_queries": ["過去場面を探す異なる観点の検索文を3〜6件"]
}
""".strip()


def read_secret(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""
    return value or os.getenv(name, "")


def make_client(api_key: str):
    return genai.Client(api_key=api_key)


def response_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    return {
        "input": int(getattr(usage, "prompt_token_count", 0) or 0),
        "output": int(getattr(usage, "candidates_token_count", 0) or 0),
        "thought": int(getattr(usage, "thoughts_token_count", 0) or 0),
        "tool": int(getattr(usage, "tool_use_prompt_token_count", 0) or 0),
        "total": int(getattr(usage, "total_token_count", 0) or 0),
    }


def generate_json(client: Any, model: str, contents: Any) -> tuple[Any, dict[str, int]]:
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return extract_json(getattr(response, "text", "") or ""), response_usage(response)


def embed_text(client: Any, text: str, role: str) -> np.ndarray:
    response = client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY" if role == "query" else "RETRIEVAL_DOCUMENT",
            output_dimensionality=768,
        ),
    )
    embeddings = getattr(response, "embeddings", []) or []
    if not embeddings:
        raise ValueError("Embeddingを取得できませんでした。")
    return np.asarray(embeddings[0].values, dtype=np.float32)


def encode_frame(frame: np.ndarray, max_width: int = 960) -> bytes:
    height, width = frame.shape[:2]
    if width > max_width:
        scale = max_width / width
        frame = cv2.resize(frame, (max_width, int(height * scale)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise ValueError("フレームを画像へ変換できませんでした。")
    return encoded.tobytes()


def video_info_and_frames(video_bytes: bytes, timestamps: list[float] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = None
    frames: list[dict[str, Any]] = []
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(video_bytes)
            path = tmp.name
        capture = cv2.VideoCapture(path)
        if not capture.isOpened():
            raise ValueError("動画を読み込めません。mp4 / movを確認してください。")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = total_frames / fps if total_frames else 0.0
        info = {"fps": fps, "duration": duration, "total_frames": total_frames}
        for timestamp in timestamps or []:
            timestamp = min(max(float(timestamp), 0.0), max(duration - 0.001, 0.0))
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(timestamp * fps))
            ok, frame = capture.read()
            if ok:
                frames.append({"time_sec": timestamp, "jpg": encode_frame(frame)})
        capture.release()
        return info, frames
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


@st.cache_data(show_spinner=False)
def inspect_video(video_bytes: bytes) -> dict[str, Any]:
    info, _ = video_info_and_frames(video_bytes)
    return info


@st.cache_data(show_spinner=False)
def extract_coarse_frames(video_bytes: bytes, interval: float, max_samples: int, cutoff: float | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    info, _ = video_info_and_frames(video_bytes)
    end = min(info["duration"], cutoff) if cutoff is not None else info["duration"]
    timestamps = np.arange(0, max(end, 0.1), interval).tolist()
    if len(timestamps) > max_samples:
        timestamps = np.linspace(0, max(end - 0.1, 0), max_samples).tolist()
    _, frames = video_info_and_frames(video_bytes, timestamps)
    for index, frame in enumerate(frames, 1):
        frame["id"] = index
        frame["start"] = max(0.0, frame["time_sec"] - interval / 2)
        frame["end"] = min(end, frame["time_sec"] + interval / 2)
    return info, frames


@st.cache_data(show_spinner=False)
def extract_detail_frames(video_bytes: bytes, center: float, radius: float, step: float, cutoff: float | None) -> list[dict[str, Any]]:
    info, _ = video_info_and_frames(video_bytes)
    upper = min(info["duration"], cutoff) if cutoff is not None else info["duration"]
    start, end = max(0.0, center - radius), min(upper, center + radius)
    # cutoffはTrigger/Payoffの開始境界なので、同時刻のフレームも過去候補に含めない。
    timestamps = [float(value) for value in np.arange(start, end + step / 2, step) if float(value) < upper - 1e-6]
    _, frames = video_info_and_frames(video_bytes, timestamps)
    return frames


def scene_document(scene: dict[str, Any], subtitle: str) -> str:
    values = [str(scene.get("search_text", ""))]
    for key in ("visual_facts", "characters", "objects", "actions", "dialogue_facts", "possible_contradictions"):
        if isinstance(scene.get(key), list):
            values.extend(str(item) for item in scene[key])
    if subtitle:
        values.append(f"字幕: {subtitle}")
    return " / ".join(value for value in values if value)


def truth_query(truth: dict[str, Any], raw_truth: str) -> str:
    values = [raw_truth, str(truth.get("payoff", {}))]
    for key in ("required_prior_conditions", "possible_triggers", "contradictions_to_seek", "retrieval_queries"):
        if isinstance(truth.get(key), list):
            values.extend(str(item) for item in truth[key])
    return " / ".join(value for value in values if value)


def truth_queries(truth: dict[str, Any], raw_truth: str) -> list[str]:
    """異なる検索意図を混ぜず、個別Embeddingとして保持する。"""
    queries: list[str] = []
    for key in ("retrieval_queries", "required_prior_conditions", "contradictions_to_seek"):
        values = truth.get(key, [])
        if isinstance(values, list):
            queries.extend(str(value).strip() for value in values if str(value).strip())
    payoff = truth.get("payoff", {})
    if isinstance(payoff, dict):
        for key in ("objects", "methods", "evidence"):
            values = payoff.get(key, [])
            if isinstance(values, list) and values:
                queries.append(" ".join(str(value) for value in values))
    queries.append(raw_truth.strip())
    return list(dict.fromkeys(query for query in queries if query))


def detail_prompt(raw_truth: str, truth: dict[str, Any], coarse: dict[str, Any], frames: list[dict[str, Any]], subtitles: str) -> str:
    times = [format_time(frame["time_sec"]) for frame in frames]
    return f"""
あなたは伏線候補の映像証拠を監査します。添付画像は時刻順で、時刻は {times} です。
粗い代表フレームだけでは見落とし得る、小道具、視線、手の動き、表情、字幕との矛盾、前後の変化を確認してください。
見えない事実を補わず、画像間で断定できない動作は不確かとしてください。
「鍵が見える」ことと「人物が鍵を持ち出した」ことを区別してください。
離れた時刻の画像を連続動作として結び付けず、人物の同一性も映像で確認できない場合は断定しないでください。

真相: {raw_truth}
構造化した真相: {truth}
粗い場面記録: {coarse.get('scene')}
該当区間の字幕: {subtitles or '字幕なし'}

次のJSONだけを返してください。
{{
  "observations": [{{"time": "MM:SS.mmm", "fact": "その画像で直接確認できた事実", "confidence": 0.0}}],
  "objects": [], "gaze_and_gestures": [], "dialogue_conflicts": [],
  "inferences_not_visually_confirmed": ["可能だが映像からは確認できない推論"],
  "evidence_strength": 0.0,
  "uncertainties": []
}}
""".strip()


def final_prompt(raw_truth: str, truth: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    compact = []
    for candidate in candidates:
        compact.append({
            "candidate_id": candidate["id"],
            "coarse_time": format_time(candidate["time_sec"]),
            "semantic_similarity": round(candidate["semantic"], 4),
            "coarse_scene": candidate["scene"],
            "detail_evidence": candidate["detail"],
            "subtitle": candidate["detail_subtitle"],
        })
    return f"""
終盤の真相から遡って、各候補を物語上の伏線として評価してください。

真相: {raw_truth}
構造化した真相: {truth}
候補: {compact}

評価基準:
- causal_score: Physical / Motivational / Psychological / Enablingの因果関係
- reinterpretation_score: 真相を知らない時と知った後で意味が変化する程度
- entity_score: 人物・物体・行動が真相と対応する程度
- evidence_score: 映像・字幕で実際に確認できる証拠の強さ
- Foreshadow: 過去の手掛かり
- Trigger: 手掛かりの意味に気づかせる情報。候補や入力から確認できなければnull
- Payoff: 終盤で伏線の意味が回収・確定する真相
- Foreshadowは候補映像内で直接確認できる事実だけを書く
- 「鍵がある」から「鍵を持ち出した」のような未撮影の行動を作らない
- Triggerが候補映像または入力から確認できなければnullとし、未来の架空の時刻を作らない
- 候補がTriggerまたはPayoffそのものならclassificationを「Trigger/Payoff」にする

同じ人物や物体が出るだけでは高得点にしないでください。次のJSON配列だけを返してください。
[
  {{
    "candidate_id": 1,
    "causal_score": 0.0, "reinterpretation_score": 0.0,
    "entity_score": 0.0, "evidence_score": 0.0,
    "causal_types": ["Enabling"],
    "interpretation_before": "真相を知らない時の意味",
    "interpretation_after": "真相を知った後の意味",
    "ftp": {{"foreshadow": "過去の手掛かり", "trigger": null, "payoff": "回収された真相"}},
    "classification": "伏線候補|ミスリード候補|単なる関連|Trigger/Payoff|根拠不足",
    "reason": "評価理由", "evidence": ["時刻付きの根拠"], "uncertainty": "不確かな点"
  }}
]
""".strip()


def friendly_error(exc: Exception) -> str:
    message = str(exc)
    if "429" in message or "RESOURCE_EXHAUSTED" in message:
        return "Gemini APIの利用上限に達しました。少し待つか、解析場面数を減らしてください。"
    if "API key" in message or "401" in message or "403" in message:
        return "Gemini APIキーまたは利用権限を確認してください。"
    if "404" in message or "not found" in message.lower():
        return "選択したGeminiモデルを利用できません。別のモデルを試してください。"
    return f"解析中にエラーが発生しました: {message}"


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items() if key not in {"jpg", "detail_frames"}}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return "<binary omitted>"
    return value


def build_export_zip(analysis: dict[str, Any]) -> tuple[bytes, str]:
    """再現・集計・閲覧に必要な研究記録を1つのZIPへまとめる。"""
    usage = merge_usage(analysis["usage"])
    cost = calculate_cost(
        usage["input"] + usage["tool"],
        usage["output"] + usage["thought"],
        analysis["model"],
        analysis["usd_jpy"],
    )
    run_id = analysis["run_id"]
    coarse_rows = []
    for rank, item in enumerate(analysis["broad"], 1):
        coarse_rows.append({
            "rank": rank,
            "time_seconds": round(float(item["time_sec"]), 3),
            "time": format_time(item["time_sec"]),
            "semantic_similarity": round(float(item["semantic"]), 6),
            "best_query": item.get("best_query", ""),
            "scene_record": item["scene"].get("search_text", ""),
        })

    detailed = []
    for rank, item in enumerate(analysis["ranked"], 1):
        judgment = item.get("judgment", {})
        detailed.append({
            "rank": rank,
            "candidate_id": item["id"],
            "start_seconds": item["detail_start"],
            "end_seconds": item["detail_end"],
            "start": format_time(item["detail_start"]),
            "end": format_time(item["detail_end"]),
            "semantic_similarity": item["semantic"],
            "best_query": item.get("best_query", ""),
            "base_score": item.get("base_score", 0),
            "role_weight": item.get("role_weight", 0),
            "final_score": item["final_score"],
            "coarse_scene": item["scene"],
            "detail_evidence": item["detail"],
            "subtitle": item["detail_subtitle"],
            "judgment": judgment,
        })

    summary = {
        "schema_version": "1.0",
        "app_version": APP_VERSION,
        "run_id": run_id,
        "created_at_local": analysis["created_at"],
        "method": "coarse_to_fine_narrative_causality",
        "video": analysis["video"],
        "settings": analysis["settings"],
        "truth_input": analysis["truth_input"],
        "truth_structure": analysis["truth"],
        "coarse_results": coarse_rows,
        "detailed_candidates": detailed,
        "api_usage": {**usage, "calls": len(analysis["usage"])},
        "estimated_cost": {**cost, "usd_jpy": analysis["usd_jpy"]},
        "processing_seconds": analysis.get("timings", {}),
        "notes": [
            "Embedding APIの使用量はusage_metadataを取得できない場合があるため概算費用に含まれないことがある。",
            "作者の意図を断定する正解ではなく、モデルによる伏線候補である。",
        ],
    }

    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=list(coarse_rows[0].keys()) if coarse_rows else ["rank"])
    writer.writeheader()
    writer.writerows(coarse_rows)

    report_parts = [
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>",
        "<title>伏線検索 実験レポート</title><style>body{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif;max-width:1000px;margin:40px auto;padding:0 24px;color:#20212a}h1,h2{color:#51308e}.card{border:1px solid #ddd8e8;border-radius:14px;padding:18px;margin:16px 0}.score{font-size:1.25rem;font-weight:700;color:#6b43b5}img{max-width:560px;border-radius:10px}pre{white-space:pre-wrap;background:#f6f4fa;padding:14px;border-radius:10px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:8px;text-align:left}</style></head><body>",
        f"<h1>粗密二段階 伏線検索レポート</h1><p>Run ID: {html.escape(run_id)}</p>",
        f"<p>動画: {html.escape(analysis['video']['name'])} / SHA-256: {html.escape(analysis['video']['sha256'])}</p>",
        f"<h2>入力した真相</h2><p>{html.escape(analysis['truth_input'])}</p>",
        f"<h2>真相の構造化</h2><pre>{html.escape(json.dumps(analysis['truth'], ensure_ascii=False, indent=2))}</pre>",
        "<h2>粗検索結果</h2><table><tr><th>順位</th><th>時刻</th><th>類似度</th><th>最も近い検索文</th><th>場面記録</th></tr>",
    ]
    for row in coarse_rows:
        report_parts.append(f"<tr><td>{row['rank']}</td><td>{row['time']}</td><td>{row['semantic_similarity']:.3f}</td><td>{html.escape(row['best_query'])}</td><td>{html.escape(row['scene_record'])}</td></tr>")
    report_parts.append("</table><h2>詳細再解析後の候補</h2>")
    for item in detailed:
        judgment = item["judgment"]
        image_path = f"evidence_frames/candidate_{item['rank']:02d}_representative.jpg"
        ftp = judgment.get("ftp", {}) if isinstance(judgment.get("ftp"), dict) else {}
        report_parts.extend([
            "<div class='card'>",
            f"<h3>候補 {item['rank']}　{item['start']}〜{item['end']}</h3>",
            f"<p class='score'>最終スコア {item['final_score']:.3f}　{html.escape(str(judgment.get('classification', '未判定')))}</p>",
            f"<img src='{image_path}' alt='候補画像'>",
            f"<p><strong>理由：</strong>{html.escape(str(judgment.get('reason', '')))}</p>",
            f"<p><strong>F：</strong>{html.escape(str(ftp.get('foreshadow') or '未確認'))}<br><strong>T：</strong>{html.escape(str(ftp.get('trigger') or '未確認'))}<br><strong>P：</strong>{html.escape(str(ftp.get('payoff') or '未確認'))}</p>",
            f"<pre>{html.escape(json.dumps(json_ready(item), ensure_ascii=False, indent=2))}</pre></div>",
        ])
    report_parts.extend([
        "<h2>API利用量</h2>",
        f"<p>呼出 {len(analysis['usage'])}回 / 入力 {usage['input']:,} / 出力 {usage['output']:,} / 思考 {usage['thought']:,} / 推定 {cost['total_jpy']:.2f}円</p>",
        f"<h2>設定</h2><pre>{html.escape(json.dumps(analysis['settings'], ensure_ascii=False, indent=2))}</pre>",
        "</body></html>",
    ])

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("run_summary.json", json.dumps(json_ready(summary), ensure_ascii=False, indent=2))
        bundle.writestr("coarse_results.csv", "\ufeff" + csv_buffer.getvalue())
        bundle.writestr("detailed_candidates.json", json.dumps(json_ready(detailed), ensure_ascii=False, indent=2))
        bundle.writestr("settings.json", json.dumps(json_ready(analysis["settings"]), ensure_ascii=False, indent=2))
        bundle.writestr("report.html", "".join(report_parts))
        for rank, item in enumerate(analysis["ranked"], 1):
            frames = item.get("detail_frames", [])
            if frames:
                representative = frames[len(frames) // 2]
                bundle.writestr(f"evidence_frames/candidate_{rank:02d}_representative.jpg", representative["jpg"])
                for frame_index, frame in enumerate(frames, 1):
                    millis = int(round(frame["time_sec"] * 1000))
                    bundle.writestr(f"evidence_frames/candidate_{rank:02d}/frame_{frame_index:02d}_{millis}ms.jpg", frame["jpg"])
    filename = f"{run_id}_coarse-to-fine.zip"
    return archive.getvalue(), filename


st.set_page_config(page_title="粗密二段階 伏線サーチ V2", page_icon="🧭", layout="wide")
st.markdown("""
<style>
.block-container{max-width:1180px;padding-top:2rem;padding-bottom:5rem}.hero{padding:1.25rem 1.5rem;border-radius:20px;background:linear-gradient(135deg,#edf4ff,#f7edff);border:1px solid #d9dced;margin-bottom:1.2rem}.stage{padding:.7rem 1rem;border-left:5px solid #7457c5;background:#f8f6ff;border-radius:8px;margin:.6rem 0}.result{padding:1rem;border:1px solid #ddd8e8;border-radius:16px;background:#fff;margin-top:1rem}.score{color:#6b43b5;font-weight:800}[data-testid="stMetricValue"]{font-size:1.28rem}
</style>
""", unsafe_allow_html=True)
st.title("🧭 粗密二段階 伏線サーチ V2")
st.markdown('<div class="hero"><b>Coarse-to-Fine Foreshadowing Retrieval with Narrative Causality</b><br>終盤の真相から過去へ遡り、粗検索した候補だけを細かく見直します。</div>', unsafe_allow_html=True)

if genai is None or types is None:
    st.error("必要なパッケージがありません。start_app.commandから起動してください。")
    st.stop()

with st.expander("このデモの10段階", expanded=False):
    st.write("動画 → 粗フレーム抽出 → 場面索引 → 真相入力 → 真相構造化 → 粗検索 → 候補再抽出 → 証拠確認 → 因果・再解釈・F–T–P判定 → 根拠と費用を表示")
    st.caption("F＝Foreshadow（過去の手掛かり）、T＝Trigger（意味に気づくきっかけ）、P＝Payoff（回収・真相の確定）")

saved_key = read_secret("GEMINI_API_KEY")
api_key = st.text_input("Gemini APIキー", type="password", placeholder="環境変数に設定済みなら空欄でOK") or saved_key
video_file = st.file_uploader("ドラマ動画", type=["mp4", "mov", "m4v", "avi", "webm"])
subtitle_file = st.file_uploader("字幕（任意・SRT）", type=["srt"])
truth_text = st.text_area("終盤で判明した真相（Payoff）", placeholder="例：田中は予備鍵で研究室へ侵入しており、指の青いインクがその証拠だった。", height=95)
video_bytes = video_file.getvalue() if video_file else b""
if video_bytes:
    st.video(video_bytes)

with st.expander("解析設定", expanded=True):
    cols = st.columns(3)
    model = cols[0].selectbox("Geminiモデル", list(MODEL_PRICES_USD), index=0)
    coarse_interval = cols[1].slider("粗抽出の間隔（秒）", 4.0, 15.0, 6.0, 1.0)
    coarse_limit = cols[2].slider("粗解析の最大場面数", 10, 120, 60, 10)
    cols = st.columns(4)
    broad_k = cols[0].slider("粗検索の候補数", 3, 15, 8)
    detail_k = cols[1].slider("詳細解析する候補数", 2, 8, 5)
    detail_radius = cols[2].slider("候補の前後（秒）", 2.0, 8.0, 4.0, 1.0)
    detail_step = cols[3].select_slider("詳細抽出の間隔", options=[1.0, 0.5], value=1.0, format_func=lambda value: f"{value}秒")
    usd_jpy = st.number_input("概算為替（1 USD＝何円）", min_value=100.0, max_value=250.0, value=150.0, step=1.0)

info = inspect_video(video_bytes) if video_bytes else None
cutoff_text = st.text_input(
    "伏線検索の終了時刻（必須）",
    value="",
    placeholder="例：00:48",
    help="Triggerまたは真相が明かされる直前を明示してください。この時刻以後は粗検索・詳細解析の両方から除外します。",
)
cutoff = parse_timecode(cutoff_text)

if video_bytes and not cutoff_text.strip():
    st.warning("⚠️ 終了時刻を指定してください。未指定のままではTrigger／Payoffが検索結果へ混入し、伏線より高得点になる可能性があります。")

if info:
    estimate = estimate_run(info["duration"] if cutoff is None else min(info["duration"], cutoff), coarse_interval, coarse_limit, detail_k, detail_radius, detail_step, model, usd_jpy)
    st.markdown('<div class="stage"><b>解析前の見積り</b>（研究用の安全側概算）</div>', unsafe_allow_html=True)
    cols = st.columns(5)
    cols[0].metric("動画時間", format_time(info["duration"]))
    cols[1].metric("粗解析", f"約{estimate['coarse_count']}場面")
    cols[2].metric("詳細画像", f"最大{estimate['detail_count']}枚")
    cols[3].metric("API呼出", f"約{estimate['api_calls']}回")
    cols[4].metric("概算料金", f"約{estimate['cost']['total_jpy']:.1f}円")

run = st.button("10段階の伏線解析を実行", type="primary", use_container_width=True, disabled=not (video_bytes and truth_text.strip() and api_key and cutoff_text.strip()))
if not api_key:
    st.info("Gemini APIキーはコードへ保存されません。画面入力またはGEMINI_API_KEY環境変数を利用します。")

run_key = hashlib.sha256(
    f"{APP_VERSION}:{hashlib.sha256(video_bytes).hexdigest()}:{truth_text}:{coarse_interval}:{coarse_limit}:{broad_k}:{detail_k}:{detail_radius}:{detail_step}:{model}:{cutoff_text}".encode()
).hexdigest() if video_bytes else ""

if run:
    try:
        if cutoff is None:
            raise ValueError("終了時刻はMM:SSまたはHH:MM:SSで入力してください。")
        if cutoff <= 0 or (info and cutoff >= info["duration"]):
            raise ValueError("終了時刻は0秒より後、動画の終了より前を指定してください。")
        subtitle_cues: list[dict[str, Any]] = []
        if subtitle_file:
            subtitle_cues = parse_srt(subtitle_file.getvalue().decode("utf-8-sig", errors="replace"))
        client = make_client(api_key)
        usages: list[dict[str, int]] = []
        timings: dict[str, float] = {}
        overall_started = time.perf_counter()

        st.markdown('<div class="stage"><b>第1段階：</b>粗い場面インデックスを作成</div>', unsafe_allow_html=True)
        stage_started = time.perf_counter()
        metadata, coarse_frames = extract_coarse_frames(video_bytes, coarse_interval, coarse_limit, cutoff)
        records, vectors = [], []
        progress = st.progress(0, text="粗い場面を解析しています")
        for index, frame in enumerate(coarse_frames, 1):
            subtitle = subtitles_for_window(subtitle_cues, frame["start"], frame["end"])
            prompt = f"{COARSE_PROMPT}\n\n代表時刻: {format_time(frame['time_sec'])}\n字幕: {subtitle or '字幕なし'}"
            scene, usage = generate_json(client, model, [types.Part.from_bytes(data=frame["jpg"], mime_type="image/jpeg"), prompt])
            if not isinstance(scene, dict):
                raise ValueError("粗い場面記録のJSONが正しくありません。")
            document = scene_document(scene, subtitle)
            vectors.append(embed_text(client, document, "document"))
            records.append({**frame, "scene": scene, "subtitle": subtitle, "document": document})
            usages.append(usage)
            progress.progress(index / len(coarse_frames), text=f"粗い場面を解析中 {index}/{len(coarse_frames)}")
        progress.empty()
        timings["coarse_index"] = round(time.perf_counter() - stage_started, 3)

        st.markdown('<div class="stage"><b>真相の構造化：</b>Payoffと検索条件を作成</div>', unsafe_allow_html=True)
        stage_started = time.perf_counter()
        truth, usage = generate_json(client, model, f"{TRUTH_PROMPT}\n\n真相:\n{truth_text.strip()}")
        usages.append(usage)
        if not isinstance(truth, dict):
            raise ValueError("真相の構造化JSONが正しくありません。")
        timings["truth_structuring"] = round(time.perf_counter() - stage_started, 3)

        st.markdown('<div class="stage"><b>粗検索：</b>Embeddingで過去の候補場面を広く取得</div>', unsafe_allow_html=True)
        stage_started = time.perf_counter()
        query_texts = truth_queries(truth, truth_text.strip())
        query_vectors = [embed_text(client, query_text, "query") for query_text in query_texts]
        score_matrix = np.vstack([cosine_scores(query_vector, np.vstack(vectors)) for query_vector in query_vectors])
        best_query_indices = np.argmax(score_matrix, axis=0)
        semantic = np.max(score_matrix, axis=0)
        for record, score, query_index in zip(records, semantic, best_query_indices):
            record["semantic"] = float(score)
            record["best_query"] = query_texts[int(query_index)]
        broad = sorted(records, key=lambda item: item["semantic"], reverse=True)[: min(broad_k, len(records))]
        detail_candidates = broad[: min(detail_k, len(broad))]
        timings["coarse_retrieval"] = round(time.perf_counter() - stage_started, 3)

        st.markdown('<div class="stage"><b>第2段階：</b>上位候補の前後だけを細かく再解析</div>', unsafe_allow_html=True)
        stage_started = time.perf_counter()
        detail_progress = st.progress(0, text="候補区間の証拠を確認しています")
        for index, candidate in enumerate(detail_candidates, 1):
            frames = extract_detail_frames(video_bytes, candidate["time_sec"], detail_radius, detail_step, cutoff)
            start = frames[0]["time_sec"] if frames else candidate["start"]
            end = frames[-1]["time_sec"] if frames else candidate["end"]
            subtitle = subtitles_for_window(subtitle_cues, start, end)
            contents: list[Any] = []
            for frame in frames:
                contents.extend([f"時刻 {format_time(frame['time_sec'])}", types.Part.from_bytes(data=frame["jpg"], mime_type="image/jpeg")])
            contents.append(detail_prompt(truth_text.strip(), truth, candidate, frames, subtitle))
            detail, usage = generate_json(client, model, contents)
            usages.append(usage)
            candidate["detail"] = detail if isinstance(detail, dict) else {}
            candidate["detail_frames"] = frames
            candidate["detail_start"] = start
            candidate["detail_end"] = end
            candidate["detail_subtitle"] = subtitle
            detail_progress.progress(index / len(detail_candidates), text=f"詳細解析中 {index}/{len(detail_candidates)}")
        detail_progress.empty()
        timings["fine_analysis"] = round(time.perf_counter() - stage_started, 3)

        st.markdown('<div class="stage"><b>物語再ランキング：</b>因果関係・再解釈度・F–T–Pを判定</div>', unsafe_allow_html=True)
        stage_started = time.perf_counter()
        judgments, usage = generate_json(client, model, final_prompt(truth_text.strip(), truth, detail_candidates))
        usages.append(usage)
        if not isinstance(judgments, list):
            raise ValueError("最終評価JSONが配列ではありません。")
        by_id = {int(item.get("candidate_id", -1)): item for item in judgments if isinstance(item, dict)}
        ranked = []
        for candidate in detail_candidates:
            judgment = by_id.get(candidate["id"], {})
            candidate["judgment"] = judgment
            candidate["base_score"] = combine_scores(candidate["semantic"], judgment.get("causal_score"), judgment.get("reinterpretation_score"), judgment.get("entity_score"), judgment.get("evidence_score"))
            candidate["final_score"] = role_adjusted_score(candidate["base_score"], judgment.get("classification"))
            candidate["role_weight"] = candidate["final_score"] / candidate["base_score"] if candidate["base_score"] else 0.0
            ranked.append(candidate)
        ranked.sort(key=lambda item: item["final_score"], reverse=True)
        timings["narrative_reranking"] = round(time.perf_counter() - stage_started, 3)
        timings["total"] = round(time.perf_counter() - overall_started, 3)
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_" + hashlib.sha256(video_bytes).hexdigest()[:8]
        settings = {
            "model": model,
            "embedding_model": EMBEDDING_MODEL,
            "search_cutoff": cutoff_text.strip(),
            "search_cutoff_seconds": cutoff,
            "coarse_interval_seconds": coarse_interval,
            "coarse_max_samples": coarse_limit,
            "coarse_candidate_count": broad_k,
            "fine_candidate_count": detail_k,
            "fine_radius_seconds": detail_radius,
            "fine_interval_seconds": detail_step,
        }
        st.session_state[run_key] = {
            "metadata": metadata,
            "truth_input": truth_text.strip(),
            "truth": truth,
            "broad": broad,
            "ranked": ranked,
            "usage": usages,
            "subtitle_count": len(subtitle_cues),
            "model": model,
            "usd_jpy": usd_jpy,
            "settings": settings,
            "timings": timings,
            "created_at": created_at,
            "run_id": run_id,
            "video": {
                "name": video_file.name if video_file else "uploaded_video",
                "sha256": hashlib.sha256(video_bytes).hexdigest(),
                "bytes": len(video_bytes),
                "duration_seconds": metadata.get("duration", 0),
            },
        }
    except Exception as exc:
        st.error(friendly_error(exc))
        with st.expander("エラー詳細"):
            st.code(str(exc))

analysis = st.session_state.get(run_key) if run_key else None
if analysis:
    st.divider()
    st.subheader("真相の構造化結果")
    st.json(analysis["truth"])

    st.subheader("粗検索結果")
    broad_rows = [{"順位": index, "時刻": format_time(item["time_sec"]), "意味類似度": round(item["semantic"], 3), "最も近かった検索文": item.get("best_query", ""), "場面記録": item["scene"].get("search_text", "")} for index, item in enumerate(analysis["broad"], 1)]
    st.dataframe(pd.DataFrame(broad_rows), use_container_width=True, hide_index=True)

    st.subheader("詳細再解析後の伏線候補")
    for rank, item in enumerate(analysis["ranked"], 1):
        judgment = item["judgment"]
        st.markdown(f'<div class="result"><b>候補 {rank}　{format_time(item["detail_start"])}〜{format_time(item["detail_end"])}</b><br><span class="score">最終スコア {item["final_score"]:.2f}</span>　{judgment.get("classification", "伏線候補")}</div>', unsafe_allow_html=True)
        left, right = st.columns([0.4, 0.6])
        with left:
            representative = item["detail_frames"][len(item["detail_frames"]) // 2] if item["detail_frames"] else item
            st.image(representative["jpg"], caption=f"代表画像 {format_time(representative['time_sec'])}", use_container_width=True)
            st.video(video_bytes, start_time=max(0, int(item["detail_start"])))
        with right:
            st.write("**理由**", judgment.get("reason", "説明なし"))
            for evidence in judgment.get("evidence", []) if isinstance(judgment.get("evidence"), list) else []:
                st.write(f"- {evidence}")
            ftp = judgment.get("ftp", {}) if isinstance(judgment.get("ftp"), dict) else {}
            st.write("**F–T–P**")
            st.write(f"F（伏線）: {ftp.get('foreshadow') or '判定できず'}")
            st.write(f"T（気づくきっかけ）: {ftp.get('trigger') or '今回の入力・映像からは未確認'}")
            st.write(f"P（回収・真相）: {ftp.get('payoff') or analysis['truth'].get('payoff', {}).get('summary', truth_text)}")
            st.write("**真相による再解釈**")
            st.caption(f"前：{judgment.get('interpretation_before', '不明')} → 後：{judgment.get('interpretation_after', '不明')}")
            with st.expander("詳細な映像証拠・スコア"):
                st.json(item["detail"])
                scores = st.columns(6)
                scores[0].metric("意味", f"{item['semantic']:.2f}")
                scores[1].metric("因果", f"{clamp_score(judgment.get('causal_score')):.2f}")
                scores[2].metric("再解釈", f"{clamp_score(judgment.get('reinterpretation_score')):.2f}")
                scores[3].metric("人物・物体", f"{clamp_score(judgment.get('entity_score')):.2f}")
                scores[4].metric("証拠", f"{clamp_score(judgment.get('evidence_score')):.2f}")
                scores[5].metric("役割補正", f"×{item.get('role_weight', 0):.1f}")
                st.caption(f"補正前スコア {item.get('base_score', 0):.3f} → 最終スコア {item['final_score']:.3f}")
                st.caption("最も近かった検索文: " + str(item.get("best_query") or "未記録"))
                st.caption("因果タイプ: " + ", ".join(judgment.get("causal_types", []) or ["未判定"]))
                st.caption("不確かな点: " + str(judgment.get("uncertainty") or "特になし"))

    usage = merge_usage(analysis["usage"])
    cost = calculate_cost(usage["input"] + usage["tool"], usage["output"] + usage["thought"], analysis["model"], analysis["usd_jpy"])
    st.subheader("Gemini API使用量と概算費用")
    cols = st.columns(6)
    cols[0].metric("生成API呼出", len(analysis["usage"]))
    cols[1].metric("入力", f"{usage['input']:,}")
    cols[2].metric("出力", f"{usage['output']:,}")
    cols[3].metric("思考", f"{usage['thought']:,}")
    cols[4].metric("ツール入力", f"{usage['tool']:,}")
    cols[5].metric("推定料金", f"{cost['total_jpy']:.2f}円")
    st.caption(f"料金表の単価と1 USD＝{analysis['usd_jpy']:.0f}円で算出。Embedding APIの使用量はusage_metadataを取得できない場合があるため、この実測額に含まれないことがあります。実請求はGoogle側を正としてください。")

    with st.expander("処理時間と再現設定"):
        timing_cols = st.columns(len(analysis.get("timings", {})) or 1)
        for column, (label, seconds) in zip(timing_cols, analysis.get("timings", {}).items()):
            column.metric(label, f"{seconds:.1f}秒")
        st.json(analysis["settings"])

    st.subheader("研究記録を保存")
    st.write("真相構造、粗検索、詳細候補、F–T–P、設定、処理時間、API利用量、証拠画像を1つのZIPへ保存します。元動画そのものは含めず、ファイル名とSHA-256を記録します。")
    export_data, export_filename = build_export_zip(analysis)
    st.download_button(
        "📦 実験結果をZIPで保存",
        data=export_data,
        file_name=export_filename,
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )
