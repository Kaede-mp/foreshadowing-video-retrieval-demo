import json
import math
import re
from typing import Any, Optional

import numpy as np


MODEL_PRICES_USD = {
    "gemini-3.7-flash": {"input": 0.75, "output": 3.75},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
}


def format_time(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    total_seconds, millis = divmod(milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def parse_timecode(value: str) -> Optional[float]:
    text = value.strip()
    if not text:
        return None
    try:
        parts = [float(part) for part in text.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def parse_srt_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d+):(\d+)[,.](\d+)", value.strip())
    if not match:
        raise ValueError(f"字幕時刻を読めません: {value}")
    hours, minutes, seconds, millis = (int(item) for item in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / (10 ** len(match.group(4)))


def parse_srt(text: str) -> list[dict[str, Any]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        try:
            start_text, end_text = [part.strip().split()[0] for part in lines[time_index].split("-->")]
            start = parse_srt_timestamp(start_text)
            end = parse_srt_timestamp(end_text)
        except (ValueError, IndexError):
            continue
        dialogue = re.sub(r"<[^>]+>", "", " ".join(lines[time_index + 1 :])).strip()
        if dialogue:
            cues.append({"start": start, "end": end, "text": dialogue})
    return cues


def subtitles_for_window(cues: list[dict[str, Any]], start: float, end: float) -> str:
    texts = [cue["text"] for cue in cues if cue["end"] >= start and cue["start"] <= end]
    return " / ".join(dict.fromkeys(texts))


def extract_json(text: str) -> Any:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        starts = [position for position in (cleaned.find("{"), cleaned.find("[")) if position >= 0]
        if not starts:
            raise
        start = min(starts)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if end <= start:
            raise
        return json.loads(cleaned[start : end + 1])


def cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    query = np.asarray(query, dtype=float)
    matrix = np.asarray(matrix, dtype=float)
    denominator = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query)
    denominator[denominator == 0] = 1
    return (matrix @ query) / denominator


def clamp_score(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def combine_scores(semantic: Any, causal: Any, reinterpretation: Any, entity: Any, evidence: Any) -> float:
    semantic_01 = clamp_score((float(semantic) + 1.0) / 2.0)
    return (
        0.25 * semantic_01
        + 0.25 * clamp_score(causal)
        + 0.20 * clamp_score(reinterpretation)
        + 0.15 * clamp_score(entity)
        + 0.15 * clamp_score(evidence)
    )


ROLE_WEIGHTS = {
    "伏線候補": 1.0,
    "ミスリード候補": 0.6,
    "単なる関連": 0.2,
    "Trigger": 0.0,
    "Payoff": 0.0,
    "Trigger/Payoff": 0.0,
    "根拠不足": 0.1,
}


def role_adjusted_score(base_score: Any, classification: Any) -> float:
    """物語上の役割を最終順位へ反映し、真相場面の上位化を防ぐ。"""
    label = str(classification or "根拠不足").strip()
    weight = ROLE_WEIGHTS.get(label, 0.1)
    return clamp_score(base_score) * weight


def merge_usage(items: list[dict[str, Any]]) -> dict[str, int]:
    keys = ("input", "output", "thought", "tool", "total")
    return {key: sum(int(item.get(key, 0) or 0) for item in items) for key in keys}


def calculate_cost(input_tokens: int, output_tokens: int, model: str, usd_jpy: float = 150.0) -> dict[str, float]:
    price = MODEL_PRICES_USD.get(model, MODEL_PRICES_USD["gemini-3.7-flash"])
    input_usd = input_tokens / 1_000_000 * price["input"]
    output_usd = output_tokens / 1_000_000 * price["output"]
    total_usd = input_usd + output_usd
    return {"input_usd": input_usd, "output_usd": output_usd, "total_usd": total_usd, "total_jpy": total_usd * usd_jpy}


def estimate_run(
    duration: float,
    coarse_interval: float,
    coarse_limit: int,
    candidate_count: int,
    detail_radius: float,
    detail_step: float,
    model: str,
    usd_jpy: float = 150.0,
) -> dict[str, Any]:
    coarse_count = min(coarse_limit, max(1, math.ceil(max(duration, 0.1) / coarse_interval)))
    detail_per_candidate = math.floor((detail_radius * 2) / detail_step) + 1
    detail_count = candidate_count * detail_per_candidate
    # Low-resolution image, prompt, JSON responseを含む安全側の研究用概算。
    input_tokens = 1_500 + coarse_count * 500 + detail_count * 380 + candidate_count * 500
    output_tokens = 1_000 + coarse_count * 220 + detail_count * 90 + candidate_count * 400
    return {
        "coarse_count": coarse_count,
        "detail_count": detail_count,
        "api_calls": coarse_count + candidate_count + 2,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": calculate_cost(input_tokens, output_tokens, model, usd_jpy),
    }
