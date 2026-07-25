from pathlib import Path

from app.config import Settings
from app.models import ClipCandidate, SubtitleCue
from app.seo import (
    complete_llm_payload,
    extract_json_object,
    normalize_llm_payload,
    select_ollama_model,
    write_youtube_metadata,
)


class FakeJob(dict):
    def keys(self):
        return super().keys()


def make_clip() -> ClipCandidate:
    return ClipCandidate(
        id="clip",
        job_id="job",
        rank=1,
        title="Viraj güvenliği",
        start=10,
        end=40,
        score=90,
        reasons=["konu netliği"],
        subtitles=[
            SubtitleCue(
                id="cue",
                start=10,
                end=15,
                text="Yağmurda viraja hızlı girmek kazaya neden olabilir.",
                words=[],
            )
        ],
    )


def test_extract_json_object_accepts_fenced_llm_output() -> None:
    payload = extract_json_object(
        '```json\n{"titles":["Başlık"],"descriptions":["Açıklama"]}\n```'
    )

    assert payload["titles"] == ["Başlık"]


def test_normalize_llm_payload_cleans_hashtags_and_tags() -> None:
    payload = normalize_llm_payload(
        {
            "titles": ["  Çok uzun olmayan başlık  "],
            "descriptions": ["Açıklama"],
            "tags": ["#Shorts", "  Yol Güvenliği  "],
            "hashtags": ["shorts, yol-güvenliği"],
            "thumbnail_texts": ["Virajda dikkat"],
            "seo_notes": ["İlk cümlede anahtar kelime kullan."],
        }
    )

    assert payload["titles"] == ["Çok uzun olmayan başlık"]
    assert payload["tags"] == ["shorts", "yol güvenliği"]
    assert payload["hashtags"] == ["#shorts", "#yolgüvenliği"]


def test_complete_llm_payload_fills_missing_seo_options() -> None:
    payload = complete_llm_payload(
        {
            "titles": ["Yağmurda Viraj Hatası"],
            "descriptions": ["Yağmurda viraj riskleri anlatılıyor."],
            "tags": ["viraj"],
            "hashtags": ["#viraj"],
            "thumbnail_texts": ["Viraj Hatası"],
            "seo_notes": ["Başlık net olsun."],
        },
        clip=make_clip(),
        text="Yağmurda viraja hızlı girmek kazaya neden olabilir.",
        keywords=["yağmur", "viraj", "kaza", "trafik"],
        source_filename="source.mp4",
    )

    assert len(payload["titles"]) == 8
    assert len(payload["descriptions"]) == 4
    assert len(payload["tags"]) == 20
    assert len(payload["hashtags"]) == 8
    assert len(payload["thumbnail_texts"]) == 5
    assert len(payload["seo_notes"]) == 6


def test_select_ollama_model_prefers_best_installed_model(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.seo.fetch_ollama_models",
        lambda settings: {"llama3.2:3b", "qwen2.5:7b"},
    )

    model = select_ollama_model(Settings(ollama_seo_model="qwen2.5:7b"))

    assert model == "qwen2.5:7b"


def test_select_ollama_model_falls_back_to_installed_model(monkeypatch) -> None:
    monkeypatch.setattr("app.seo.fetch_ollama_models", lambda settings: {"llama3.2:3b"})

    model = select_ollama_model(Settings(ollama_seo_model="qwen2.5:7b"))

    assert model == "llama3.2:3b"


def test_write_youtube_metadata_uses_ollama_when_available(tmp_path: Path, monkeypatch) -> None:
    def fake_generate(**kwargs):
        return (
            {
                "titles": ["Yağmurda Viraj Hatası"],
                "descriptions": ["Yağmurda viraja hızlı girmenin riskleri anlatılıyor."],
                "tags": ["shorts", "viraj", "trafik güvenliği"],
                "hashtags": ["#shorts", "#trafik"],
                "thumbnail_texts": ["Virajda Hata"],
                "seo_notes": ["Başlıkta ana konuyu açık yaz."],
            },
            None,
            "qwen2.5:7b",
        )

    monkeypatch.setattr("app.seo.generate_ollama_seo", fake_generate)
    destination = tmp_path / "youtube-seo.md"

    write_youtube_metadata(
        job=FakeJob(filename="source.mp4"),
        clip=make_clip(),
        export_id="export",
        output=tmp_path / "short.mp4",
        destination=destination,
        settings=Settings(data_dir=tmp_path, ollama_seo_model="qwen2.5:7b"),
        llm_seo_enabled=True,
    )

    content = destination.read_text(encoding="utf-8")
    assert "SEO üretici: `Ollama qwen2.5:7b`" in content
    assert "Yağmurda Viraj Hatası" in content
    assert "### Önerilen Kopyala-Yapıştır Paket" in content
    assert "## Yerel Yedek Başlık Seçenekleri" in content


def test_write_youtube_metadata_does_not_call_ollama_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_generate(**kwargs):
        raise AssertionError("LLM SEO should be opt-in")

    monkeypatch.setattr("app.seo.generate_ollama_seo", fail_generate)
    destination = tmp_path / "youtube-seo.md"

    write_youtube_metadata(
        job=FakeJob(filename="source.mp4"),
        clip=make_clip(),
        export_id="export",
        output=tmp_path / "short.mp4",
        destination=destination,
        settings=Settings(data_dir=tmp_path, ollama_seo_model="qwen2.5:7b"),
    )

    content = destination.read_text(encoding="utf-8")
    assert "SEO üretici: `Yerel yedek`" in content
    assert "Profesyonel LLM SEO kapalı" in content
