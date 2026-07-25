from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from app.config import Settings
from app.models import ClipCandidate


TURKISH_STOPWORDS = {
    "acaba",
    "ama",
    "artık",
    "aslında",
    "az",
    "bana",
    "bazen",
    "belki",
    "ben",
    "beni",
    "benim",
    "beri",
    "beş",
    "bile",
    "bir",
    "biraz",
    "biri",
    "biz",
    "bize",
    "bizi",
    "bizim",
    "bu",
    "buna",
    "bunda",
    "bundan",
    "bunu",
    "bunun",
    "çok",
    "çünkü",
    "da",
    "daha",
    "de",
    "defa",
    "diye",
    "en",
    "gibi",
    "hem",
    "hep",
    "hepsi",
    "her",
    "hiç",
    "için",
    "ile",
    "ise",
    "işte",
    "kaç",
    "ki",
    "kim",
    "mı",
    "mi",
    "mu",
    "mü",
    "nasıl",
    "ne",
    "neden",
    "nerde",
    "nerede",
    "nereye",
    "niye",
    "o",
    "olan",
    "olarak",
    "oldu",
    "oluyor",
    "onu",
    "onun",
    "orada",
    "öyle",
    "şey",
    "şimdi",
    "şu",
    "tabi",
    "tam",
    "ve",
    "veya",
    "ya",
    "yani",
    "yok",
}

LLM_SYSTEM_PROMPT = """Sen Türkçe YouTube Shorts için çalışan profesyonel SEO stratejisti ve copywriter'sın.
Görev: verilen kısa video transkriptinden tıklanabilir ama yanıltıcı olmayan başlıklar,
açıklamalar, etiketler, hashtagler ve kapak yazıları üret.
Sadece geçerli JSON döndür. Markdown, açıklama veya kod bloğu ekleme.
JSON şeması:
{
  "titles": ["tam 8 seçenek, her biri 45-70 karakter"],
  "descriptions": ["tam 4 seçenek, her biri 2-4 paragraf YouTube açıklaması"],
  "tags": ["tam 20 kısa etiket"],
  "hashtags": ["tam 8 hashtag"],
  "thumbnail_texts": ["tam 5 kısa kapak yazısı"],
  "seo_notes": ["tam 6 kısa uygulanabilir not"]
}
Kurallar:
- Dil Türkçe olmalı.
- Başlıklar SEO anahtar kelimesi içermeli ama spam gibi görünmemeli.
- Başlıkların ilk 35 karakteri güçlü olmalı.
- Merak uyandır, ama transkriptte olmayan iddia uydurma.
- Başlıklarda aynı kalıbı tekrar etme.
- Açıklamalar YouTube'a direkt yapıştırılabilecek kalitede olmalı.
- Açıklamalarda ilk satır güçlü hook, ikinci bölüm doğal özet, son bölüm yorum/abonelik çağrısı olmalı.
- "Bu kısa videoda", "dikkat çeken kısım", "videonun tamamından seçilen" gibi jenerik kalıpları kullanma.
- Kaynak dosya adı, klip zamanı veya teknik export bilgisini açıklama metnine koyma.
- Etiketler YouTube etiket alanına uygun, virgülsüz tek tek ifadeler olmalı.
- Hashtagler # ile başlamalı.
- Sadece JSON döndür."""

PREFERRED_OLLAMA_MODELS = ("qwen2.5:7b", "llama3.1:8b", "llama3.2:3b")

MIN_LLM_TITLES = 8
MIN_LLM_DESCRIPTIONS = 4
MIN_LLM_TAGS = 20
MIN_LLM_HASHTAGS = 8
MIN_LLM_THUMBNAILS = 5
MIN_LLM_NOTES = 6


def clip_text(clip: ClipCandidate) -> str:
    return " ".join(cue.text.strip() for cue in clip.subtitles if cue.text.strip())


def markdown_escape(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def tokenize(text: str) -> list[str]:
    return [
        token.casefold()
        for token in re.findall(r"[0-9A-Za-zÇĞİÖŞÜçğıöşü'-]+", text)
        if len(token) > 2 and token.casefold() not in TURKISH_STOPWORDS
    ]


def top_keywords(text: str, limit: int = 14) -> list[str]:
    counts = Counter(tokenize(text))
    return [word for word, _ in counts.most_common(limit)]


def readable_phrase(text: str, max_words: int = 8) -> str:
    words = re.findall(r"[0-9A-Za-zÇĞİÖŞÜçğıöşü'-]+", text)
    phrase = " ".join(words[:max_words]).strip()
    return phrase[:1].upper() + phrase[1:] if phrase else "Öne çıkan an"


def trim_title(title: str, limit: int = 70) -> str:
    title = re.sub(r"\s+", " ", title).strip(" -|")
    if len(title) <= limit:
        return title
    trimmed = title[: limit - 1].rsplit(" ", 1)[0]
    return f"{trimmed}…"


def sentence_candidates(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text)
    return [part.strip() for part in parts if len(part.strip()) > 12]


def title_options(clip: ClipCandidate, text: str, keywords: list[str]) -> list[str]:
    lead = readable_phrase(text, 7)
    primary = keywords[0].title() if keywords else "Shorts"
    secondary = keywords[1].title() if len(keywords) > 1 else "Video"
    tertiary = keywords[2].title() if len(keywords) > 2 else "Kısa Video"
    options = [
        trim_title(f"{lead}: bu detay sonucu değiştiriyor"),
        trim_title(f"{primary} konusunda çoğu kişinin kaçırdığı nokta"),
        trim_title(f"{secondary} neden riskli? Kısa ama net açıklama"),
        trim_title(f"{clip.title}: izleyince fikrin değişebilir"),
        trim_title(f"{primary} meselesinde asıl kritik hata ne?"),
        trim_title(f"{primary} ve {secondary}: gözden kaçan gerçek"),
        trim_title(f"{tertiary} hakkında bilmen gereken uyarı"),
        trim_title(f"Bu bölüm {primary} konusunu netleştiriyor"),
        trim_title(f"{secondary} hakkında düşündüren kısa analiz"),
        trim_title(f"{primary}: Shorts için en güçlü bölüm"),
    ]
    unique: list[str] = []
    for option in options:
        if option not in unique:
            unique.append(option)
    return unique


def extend_unique(current: list[str], fallback: list[str], limit: int) -> list[str]:
    values: list[str] = []
    for item in [*current, *fallback]:
        normalized = re.sub(r"\s+", " ", item).strip()
        if normalized and normalized.casefold() not in {value.casefold() for value in values}:
            values.append(normalized)
        if len(values) >= limit:
            break
    return values


def description_options(
    _source_filename: str,
    _clip: ClipCandidate,
    text: str,
    keywords: list[str],
) -> list[str]:
    sentences = sentence_candidates(text)
    summary = " ".join(sentences[:2]) if sentences else readable_phrase(text, 18)
    keyword_line = ", ".join(keywords[:6])
    primary = keywords[0] if keywords else "bu konu"
    secondary = keywords[1] if len(keywords) > 1 else "detaylar"
    hook = trim_title(f"{primary.title()} konusunda gözden kaçan kritik detay", 95)
    return [
        (
            f"{hook}\n\n"
            f"{summary}\n\n"
            "Sen bu konuda ne düşünüyorsun? Yorumlara yaz.\n\n"
            f"Ana konular: {keyword_line}\n\n#shorts #keşfet"
        ),
        (
            f"{secondary.title()} tarafında yapılan küçük bir hata büyük sonuç doğurabilir.\n\n"
            f"{summary}\n\n"
            "Benzer içerikler için takip etmeyi unutma.\n\n"
            f"Etiket odağı: {keyword_line}\n\n#shorts #viral"
        ),
        (
            f"{readable_phrase(text, 14)}\n\n"
            f"Bu bölümde {primary} ve {secondary} konusu kısa, net ve anlaşılır şekilde öne çıkıyor.\n\n"
            "Fikrini yorumlarda paylaş.\n\n#shorts #youtubeShorts"
        ),
    ]


def hashtags(keywords: list[str]) -> list[str]:
    values = ["shorts", "keşfet", "viral", "youtubeShorts"]
    for keyword in keywords[:5]:
        clean = re.sub(r"[^0-9A-Za-zÇĞİÖŞÜçğıöşü]+", "", keyword)
        if clean:
            values.append(clean)
    return [f"#{value}" for value in values]


def tag_options(keywords: list[str], clip: ClipCandidate) -> list[str]:
    keyword_phrases = []
    for keyword in keywords[:8]:
        keyword_phrases.extend([keyword, f"{keyword} shorts", f"{keyword} video"])
    tags = [
        "shorts",
        "youtube shorts",
        "kısa video",
        "viral shorts",
        "türkçe shorts",
        "shorts keşfet",
        "keşfet",
        "reels",
        "tiktok",
        "viral video",
        "trend video",
        "türkçe video",
        *keyword_phrases,
        *[reason.casefold() for reason in clip.reasons],
    ]
    unique: list[str] = []
    for tag in tags:
        tag = re.sub(r"\s+", " ", tag).strip(" ,#").casefold()
        if tag and tag not in unique:
            unique.append(tag)
    return unique[:24]


def ollama_model_candidates(settings: Settings) -> list[str]:
    candidates: list[str] = []
    for model in (settings.ollama_seo_model, *PREFERRED_OLLAMA_MODELS):
        normalized = model.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def fetch_ollama_models(settings: Settings) -> set[str]:
    endpoint = urljoin(settings.ollama_base_url.rstrip("/") + "/", "api/tags")
    request = urllib.request.Request(endpoint, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.loads(response.read().decode("utf-8"))
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        return set()
    names: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        for key in ("name", "model"):
            value = model.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
    return names


def select_ollama_model(settings: Settings) -> str:
    candidates = ollama_model_candidates(settings)
    try:
        installed_models = fetch_ollama_models(settings)
    except (OSError, TimeoutError, json.JSONDecodeError, urllib.error.URLError, urllib.error.HTTPError):
        return candidates[0]
    for candidate in candidates:
        if candidate in installed_models:
            return candidate
    return candidates[0]


def ollama_generate(
    *,
    settings: Settings,
    model: str,
    prompt: str,
) -> str:
    endpoint = urljoin(settings.ollama_base_url.rstrip("/") + "/", "api/generate")
    payload = {
        "model": model,
        "prompt": prompt,
        "system": LLM_SYSTEM_PROMPT,
        "stream": False,
        "keep_alive": "15m",
        "options": {
            "temperature": 0.7,
            "top_p": 0.9,
            "num_predict": 1400,
        },
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=settings.ollama_timeout_seconds) as response:
        body = json.loads(response.read().decode("utf-8"))
    generated = str(body.get("response") or "").strip()
    if not generated:
        raise RuntimeError("Ollama boş cevap döndürdü.")
    return generated


def readable_ollama_error(exc: Exception, settings: Settings, model: str) -> str:
    message = str(exc).strip()
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        if "not found" in body.casefold() or "pull" in body.casefold():
            return (
                f"Ollama modeli `{model}` yüklü değil. "
                f"PowerShell'de `ollama pull {model}` çalıştırıp tekrar dışa aktar."
            )
        message = body.strip() or message
    if isinstance(exc, TimeoutError) or "timed out" in message.casefold():
        return (
            "Ollama cevap süresi doldu. "
            f"Model `{model}` {settings.ollama_timeout_seconds:.0f} saniye içinde SEO çıktısını tamamlayamadı. "
            "`OLLAMA_TIMEOUT_SECONDS` değerini yükseltebilir veya daha küçük/hızlı bir Ollama modeli kullanabilirsin."
        )
    return message or exc.__class__.__name__


def extract_json_object(value: str) -> dict[str, Any]:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first == -1 or last == -1 or last <= first:
        raise ValueError("LLM cevabında JSON nesnesi bulunamadı.")
    payload = json.loads(cleaned[first : last + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM cevabı JSON nesnesi değil.")
    return payload


def clean_list(value: Any, limit: int, item_limit: int = 240) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item)).strip()
        if not text or text in cleaned:
            continue
        cleaned.append(text[:item_limit].strip())
        if len(cleaned) >= limit:
            break
    return cleaned


def normalize_hashtags(values: list[str]) -> list[str]:
    hashtags_list: list[str] = []
    for value in values:
        for item in re.split(r"[\s,]+", value):
            clean = re.sub(r"[^0-9A-Za-zÇĞİÖŞÜçğıöşü#]+", "", item).strip()
            if not clean:
                continue
            if not clean.startswith("#"):
                clean = f"#{clean}"
            if clean not in hashtags_list:
                hashtags_list.append(clean)
    return hashtags_list[:8]


def normalize_llm_payload(payload: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "titles": [trim_title(title) for title in clean_list(payload.get("titles"), 12, 100)],
        "descriptions": clean_list(payload.get("descriptions"), 6, 1200),
        "tags": [
            re.sub(r"\s+", " ", tag).strip(" ,#").casefold()
            for tag in clean_list(payload.get("tags"), 30, 60)
            if tag.strip(" ,#")
        ],
        "hashtags": normalize_hashtags(clean_list(payload.get("hashtags"), 16, 40)),
        "thumbnail_texts": [
            trim_title(text, 34) for text in clean_list(payload.get("thumbnail_texts"), 8, 60)
        ],
        "seo_notes": clean_list(payload.get("seo_notes"), 8, 180),
    }


def complete_llm_payload(
    payload: dict[str, list[str]],
    *,
    clip: ClipCandidate,
    text: str,
    keywords: list[str],
    source_filename: str,
) -> dict[str, list[str]]:
    local_titles = title_options(clip, text, keywords)
    local_descriptions = description_options(source_filename, clip, text, keywords)
    local_tags = tag_options(keywords, clip)
    local_hashtags = hashtags(keywords)
    local_thumbnails = [trim_title(title, 34) for title in local_titles]
    local_notes = [
        "Başlıkta ana konuyu ilk 35 karakterde kullan.",
        "Açıklamanın ilk cümlesinde anahtar kelime ve merak unsuru olsun.",
        "Hashtagleri açıklamanın sonuna ekle.",
        "Etiketlerde hem genel hem konuya özel ifadeler kullan.",
        "Kapak yazısını 2-4 kelimeyle kısa tut.",
        "Başlık ve kapak yazısı aynı cümleyi tekrar etmesin.",
    ]
    return {
        "titles": extend_unique(payload["titles"], local_titles, MIN_LLM_TITLES),
        "descriptions": extend_unique(
            payload["descriptions"],
            local_descriptions,
            MIN_LLM_DESCRIPTIONS,
        ),
        "tags": extend_unique(payload["tags"], local_tags, MIN_LLM_TAGS),
        "hashtags": extend_unique(payload["hashtags"], local_hashtags, MIN_LLM_HASHTAGS),
        "thumbnail_texts": extend_unique(
            payload["thumbnail_texts"],
            local_thumbnails,
            MIN_LLM_THUMBNAILS,
        ),
        "seo_notes": extend_unique(payload["seo_notes"], local_notes, MIN_LLM_NOTES),
    }


def build_llm_prompt(
    *,
    source_filename: str,
    clip: ClipCandidate,
    text: str,
    local_keywords: list[str],
) -> str:
    transcript = text[:6000]
    return (
        "Aşağıdaki YouTube Shorts klibi için SEO paketi üret.\n\n"
        f"Kaynak dosya: {source_filename}\n"
        f"Klip sırası: {clip.rank}\n"
        f"Kaynak zamanı: {clip.start:.2f} - {clip.end:.2f}\n"
        f"Yerel anahtar kelimeler: {', '.join(local_keywords[:12])}\n"
        f"Seçilme nedenleri: {', '.join(clip.reasons)}\n\n"
        "Zorunlu çıktı adetleri:\n"
        "- titles: tam 8 başlık\n"
        "- descriptions: tam 4 açıklama\n"
        "- tags: tam 20 etiket\n"
        "- hashtags: tam 8 hashtag\n"
        "- thumbnail_texts: tam 5 kapak yazısı\n"
        "- seo_notes: tam 6 SEO notu\n\n"
        "Başlık stilleri karışık olsun: soru, uyarı, merak, açıklayıcı, tartışmalı ama yanıltıcı olmayan.\n"
        "Başlıklar YouTube aramasında bulunabilir olmalı; sadece merak cümlesi yazma, ana konuyu da geçir.\n"
        "Açıklama formatı şu mantıkta olsun: 1 güçlü hook satırı, 1 doğal özet paragrafı, 1 yorum/takip çağrısı, son satır hashtagler.\n"
        "Açıklamalarda kaynak dosya adı, zaman kodu, export bilgisi veya teknik işlem detayı kullanma.\n"
        "Klipe uymayan büyük iddialar, clickbait yalanları ve yapay pazarlama dili kullanma.\n"
        "Yasak jenerik ifadeler: Bu kısa videoda, dikkat çeken kısım, videonun tamamından seçilen, Shorts için hazırlandı.\n\n"
        "Transkript:\n"
        f"{transcript}\n\n"
        "Önce transkriptin ana fikrini çıkar, ama analizini JSON'a koyma. "
        "Başlıklar doğal Türkçe, net ve profesyonel olsun. "
        "Açıklamalar YouTube'a direkt yapıştırılabilir kalitede olsun. "
        "Etiketlerde hem genel hem konuya özel kelimeler olsun."
    )


def generate_ollama_seo(
    *,
    settings: Settings,
    source_filename: str,
    clip: ClipCandidate,
    text: str,
    local_keywords: list[str],
) -> tuple[dict[str, list[str]] | None, str | None, str]:
    model = settings.ollama_seo_model
    if not settings.ollama_seo_enabled:
        return None, "Ollama SEO kapalı.", model
    prompt = build_llm_prompt(
        source_filename=source_filename,
        clip=clip,
        text=text,
        local_keywords=local_keywords,
    )
    try:
        model = select_ollama_model(settings)
        generated = ollama_generate(settings=settings, model=model, prompt=prompt)
        payload = complete_llm_payload(
            normalize_llm_payload(extract_json_object(generated)),
            clip=clip,
            text=text,
            keywords=local_keywords,
            source_filename=source_filename,
        )
    except (
        OSError,
        TimeoutError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
        urllib.error.HTTPError,
    ) as exc:
        return None, readable_ollama_error(exc, settings, model), model
    if len(payload["titles"]) < 3 or len(payload["descriptions"]) < 1:
        return None, "Ollama cevabı yeterli başlık/açıklama içermedi.", model
    return payload, None, model


def write_youtube_metadata(
    *,
    job: Any,
    clip: ClipCandidate,
    export_id: str,
    output: Path,
    destination: Path,
    settings: Settings,
    llm_seo_enabled: bool = False,
) -> None:
    text = clip_text(clip)
    keywords = top_keywords(text)
    source_filename = str(job["filename"] if "filename" in job.keys() else output.name)
    titles = title_options(clip, text, keywords)
    descriptions = description_options(source_filename, clip, text, keywords)
    tags = tag_options(keywords, clip)
    hash_values = hashtags(keywords)
    if llm_seo_enabled:
        llm_payload, llm_error, llm_model = generate_ollama_seo(
            settings=settings,
            source_filename=source_filename,
            clip=clip,
            text=text,
            local_keywords=keywords,
        )
    else:
        llm_payload = None
        llm_error = "Profesyonel LLM SEO bu dışa aktarmada kapalı."
        llm_model = settings.ollama_seo_model

    effective_duration = (
        clip.end
        - clip.start
        - sum(cut.end - cut.start for cut in clip.cut_ranges)
        + sum(insert.source_end - insert.source_start for insert in clip.insert_ranges)
    )
    markdown = [
        "# YouTube SEO Paketi",
        "",
        f"- Export ID: `{export_id}`",
        f"- Video dosyası: `{output.name}`",
        f"- Kaynak dosya: `{source_filename}`",
        f"- Klip: `{clip.rank}`",
        f"- Kaynak zaman: `{clip.start:.2f}` - `{clip.end:.2f}`",
        f"- Tahmini süre: `{effective_duration:.2f} sn`",
        f"- SEO üretici: `{'Ollama ' + llm_model if llm_payload else 'Yerel yedek'}`",
        "",
    ]
    if clip.insert_ranges:
        markdown.extend(
            [
                "## Eklenen Kaynak Parçaları",
                "",
                *[
                    (
                        f"- `{insert.source_start:.2f}` - `{insert.source_end:.2f}` "
                        f"aralığı `{insert.insert_at:.2f}` noktasından önce"
                    )
                    for insert in clip.insert_ranges
                ],
                "",
            ]
        )
    if llm_payload:
        best_title = llm_payload["titles"][0]
        best_description = llm_payload["descriptions"][0]
        markdown.extend(
            [
                "## LLM SEO Önerileri",
                "",
                f"Model: `{llm_model}`",
                "",
                "### Önerilen Kopyala-Yapıştır Paket",
                "",
                "**Başlık**",
                "",
                markdown_escape(best_title),
                "",
                "**Açıklama**",
                "",
                markdown_escape(best_description),
                "",
                "**Etiketler**",
                "",
                ", ".join(llm_payload["tags"]),
                "",
                "**Hashtagler**",
                "",
                " ".join(llm_payload["hashtags"]),
                "",
                "### Başlık Seçenekleri",
                "",
                *[
                    f"{index}. {markdown_escape(title)}"
                    for index, title in enumerate(llm_payload["titles"], 1)
                ],
                "",
                "### Açıklama Seçenekleri",
                "",
            ]
        )
        for index, description in enumerate(llm_payload["descriptions"], 1):
            markdown.extend(
                [
                    f"#### Açıklama {index}",
                    "",
                    markdown_escape(description),
                    "",
                ]
            )
        markdown.extend(
            [
                "### Etiketler",
                "",
                ", ".join(llm_payload["tags"]),
                "",
                "### Hashtagler",
                "",
                " ".join(llm_payload["hashtags"]),
                "",
                "### Thumbnail / Kapak Yazısı Önerileri",
                "",
                *[f"- {markdown_escape(text)}" for text in llm_payload["thumbnail_texts"]],
                "",
                "### SEO Notları",
                "",
                *[f"- {markdown_escape(note)}" for note in llm_payload["seo_notes"]],
                "",
            ]
        )
    else:
        markdown.extend(
            [
                "## LLM SEO Önerileri",
                "",
                (
                    "Profesyonel LLM SEO kapalı olduğu için yerel yedek öneriler kullanıldı."
                    if not llm_seo_enabled
                    else "Ollama ile LLM üretimi yapılamadı; yerel yedek öneriler kullanıldı."
                ),
                "",
                f"- Model: `{llm_model}`",
                f"- Hata: `{markdown_escape(llm_error or 'Bilinmeyen hata')}`",
                "",
            ]
        )

    markdown.extend(
        [
        "## Yerel Yedek Başlık Seçenekleri",
        "",
        *[f"{index}. {markdown_escape(title)}" for index, title in enumerate(titles, 1)],
        "",
        "## Yerel Yedek Açıklama Seçenekleri",
        "",
        ]
    )
    for index, description in enumerate(descriptions, 1):
        markdown.extend(
            [
                f"### Açıklama {index}",
                "",
                markdown_escape(description),
                "",
            ]
        )
    markdown.extend(
        [
            "## Yerel Yedek Etiketler",
            "",
            ", ".join(tags),
            "",
            "## Yerel Yedek Hashtagler",
            "",
            " ".join(hash_values),
            "",
            "## Yerel Yedek Thumbnail / Kapak Yazısı Önerileri",
            "",
            *[f"- {trim_title(title, 34)}" for title in titles[:3]],
            "",
            "## SEO Notları",
            "",
            "- İlk başlığı YouTube başlığı için kullan.",
            "- Açıklamada ilk 1-2 cümle en önemli anahtar kelimeleri içermeli.",
            "- Etiketleri virgülle ayırarak YouTube etiket alanına ekleyebilirsin.",
            "- Hashtagleri açıklamanın sonuna koymak daha temiz görünür.",
            "",
            "## Klip Metni",
            "",
            markdown_escape(text) or "_Metin bulunamadı._",
            "",
        ]
    )
    destination.write_text("\n".join(markdown), encoding="utf-8")
