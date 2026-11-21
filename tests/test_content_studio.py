from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.codex_runner import CodexError
from app.config import Settings
from app.main import create_app
from app.transcription import TranscriptionError
from app.youtube_import import YoutubeDownload, YoutubeImportError


OPTIONS_URL = "/api/content-studio/options"
UPLOAD_URL = f"{OPTIONS_URL}/video"
GENERATE_URL = "/api/content-studio/generate"
HISTORY_URL = "/api/content-studio/history"
VIDEO_URL = "https://www.youtube.com/watch?v=abcdefghijk"
BRIEF = "Forza şehir içinde park ederken ağır geliyor. Uzun yol için NX500'ü denemek istiyorum."
TRANSCRIPT = "Geçen sene memleket turuna çıkmıştım. Dönüşte motoru satmaya karar verdim."
TARGET_SECONDS = 90
OPTIONS = {
    "reference_insights": "Karar, gündelik bir anıyla açılıyor.",
    "target_seconds": TARGET_SECONDS,
    "options": [
        {
            "id": "park",
            "title": "Park yerinden başla",
            "hook": "Bu motorun ağırlığı en çok şu park yerinde aklıma geliyor.",
            "approach": "Park manevrasını şehirdeki zorluğun örneği olarak anlat.",
            "premise": "Bir park manevrası, şehir için daha hafif motor arayışını başlatır.",
            "payoff": "İzleyici düşük hızda hangi hareketleri deneyerek karar vereceğini görür.",
            "tradeoff": "Tek kişisel sahne anlatıyı yakınlaştırır. Uzun yol beklentisi sonradan açılır.",
            "beats": [
                {
                    "title": "Parktan çıkış",
                    "story": "Geri çekişteki ağırlık hissini, normal sürüş rahatlığından ayır.",
                    "visual": "Ayağın ve arka tekerin kadraja girdiği düşük açı.",
                    "transition": "Manevra bitince ağırlığın sürüşte ne zaman işe yaradığını sor.",
                },
                {
                    "title": "Uzun yolda aradığım",
                    "story": "Saatlerce aynı oturuşta kaldığında önem kazanan konforu aç.",
                    "visual": "Forza'nın selesine ve gidon oturuşuna sabit kadraj.",
                    "transition": "Farklı kullanımın getirdiği iki önceliği test ölçütüne çevir.",
                },
                {
                    "title": "İlk test",
                    "story": "NX500 ile aynı manevrayı yapabilmeyi henüz açık bir soru olarak bırak.",
                    "visual": "Günlük park yerinin boş görüntüsü üzerine test ölçütünü yaz.",
                    "transition": "İzleyiciye kendi günlük manevrasını ve hangi ölçütü seçeceğini sor.",
                },
            ],
        },
        {
            "id": "rota",
            "title": "Bir şehir, bir uzun yol",
            "hook": "Her gün işime gittiğim motorla hafta sonu yola da çıkmak istiyorum.",
            "approach": "Şehir içi ve şehir dışı rotayı yan yana koy.",
            "premise": "Aynı motor için günlük ihtiyaçlarla uzun yol beklentileri çarpışıyor.",
            "payoff": "İzleyici kendi şehir ve uzun yol kullanımını iki ayrı ölçütle tartabilir.",
            "tradeoff": "İki rota dengeli görünür. Bunun için park ve yol görüntüsü ayrı çekilir.",
            "beats": [
                {
                    "title": "Şehir içi",
                    "story": "Bir rotanın motor keyfini güvenli ve rahat park edebilmenin nasıl etkilediğini anlat.",
                    "visual": "Eğimli park yerinde motosikletten inip geri almak için hazırlandığın geniş plan.",
                    "transition": "Yol içinde konforun, park anında ise kontrolün öne çıktığını söyle.",
                },
                {
                    "title": "Şehir dışı",
                    "story": "Uzun yolda oturuşun ve motorun tepkisinin neden önem kazandığını aç.",
                    "visual": "Şehirden çıkış tabelası ve oturuş pozisyonunun sabit kamerayla kesiti.",
                    "transition": "Karar ölçütlerini toparlayıp test sürüşünde neyi deneyeceğini söyle.",
                },
                {
                    "title": "Tek motor sorusu",
                    "story": "NX500 denemesi için iki ölçüt belirle. Kararı test sonrasına bırak.",
                    "visual": "Kameraya bakarak konuş ve ekrana iki ölçütü sırayla getir.",
                    "transition": "Soruyu tek planda sorup iki saniye bekle.",
                },
            ],
        },
        {
            "id": "test",
            "title": "Test sürüşü öncesi",
            "hook": "NX500'ü denerken ilk bakacağım şey kağıtta yazmıyor.",
            "approach": "Test sürüşünde cevap aranacak üç soruyla ilerle.",
            "premise": "Model seçimi, kâğıt üzerindeki özelliklerden pratik bir deneme planına dönüşür.",
            "payoff": "İzleyici test sürüşünde kontrol edebileceği somut hareketleri not eder.",
            "tradeoff": "Çekilecek ilk video bir test planı sunar. Deneyimin sonucu ikinci videoya kalır.",
            "beats": [
                {
                    "title": "Geri manevra",
                    "story": "Ayakların yere rahat basıp basmadığını geri çekişte denemeyi planla.",
                    "visual": "Park girişini çek ve testte kullanılacak hareketi Forza ile göster.",
                    "transition": "Parkta ayak kontrolünden düşük hızdaki gidon kontrolüne geç.",
                },
                {
                    "title": "Yavaş dönüş",
                    "story": "Kalkış ve dönüş hissini, düz yoldaki rahat sürüşten ayrı değerlendir.",
                    "visual": "Güvenli, boş bir dönüş alanının genel planı.",
                    "transition": "Şehirde kısa süren hareketleri uzun yolda sürekli hissedilen oturuşla karşılaştır.",
                },
                {
                    "title": "Yol oturuşu",
                    "story": "NX500 için uzun süre aynı pozisyonda kalmanın nasıl hissettireceğini soru olarak bırak.",
                    "visual": "Motosiklet dururken oturuşun yandan çekilmiş profili.",
                    "transition": "Üç test ölçütünü özetleyip ilk denemede bakılacak önceliği sor.",
                },
            ],
        },
    ],
}
BLUEPRINT = {
    "target_seconds": TARGET_SECONDS,
    "editorial_direction": "İki rotayı karşılaştır ve kararı gerçek test sürüşüne bırak.",
    "source_facts": ["Forza şehir içinde park ederken ağır geliyor.", "NX500 henüz denenmedi."],
    "scenes": [
        {
            **beat,
            "duration_seconds": 30,
            "evidence": "Brief şehir içinde parkı ve uzun yol beklentisini veriyor.",
        }
        for beat in OPTIONS["options"][1]["beats"]
    ],
}
WRITING = {
    "edit_plan": [
        {
            "beat": "Şehir içi",
            "spoken": "Her gün işime gittiğim motorla hafta sonu yola da çıkmak istiyorum. "
            "Forza'yı park yerinden geri alırken ağırlığını hissediyorum. "
            "Tam da ayağımla geriye doğru ilk adımı attığım anda kararım değişebiliyor. "
            "Yolda giderken keyfim yerinde ama motoru bir yere bırakacağım zaman işin içine başka bir ölçü giriyor. "
            "O yüzden şehirde kullanım benim için yalnızca trafikte rahat gitmek demek değil.",
            "visual": "Eğimli park yeri. Sürücünün iki ayağını ve motosikleti aynı kadraja al.",
            "edit": "Geri manevranın başını ve sonunu geniş planda göster.",
            "audio": "Ayak sesi gelene kadar müziği geri çek.",
            "on_screen": "Günlük ölçüt · park",
            "transition": "Motor düz zemine gelince şehirden çıkış planına kes.",
        },
        {
            "beat": "Şehir dışı",
            "spoken": "Şehirden çıkınca kafamdaki soru değişiyor. "
            "Bu sefer park yerini düşünmek yerine yolda ne kadar rahat edeceğimi düşünmeye başlıyorum. "
            "Bir noktadan sonra oturuş rahatlığı her gün yaptığım kısa sürüştekinden daha önemli gelebiliyor. "
            "Ağırlığı yalnızca bir rakam gibi okuyunca bu tarafı kaçırıyorum. "
            "Bunun için yeni motoru düşünürken günlük parkımı da uzun yolumu da ayrı ayrı hesaba katıyorum.",
            "visual": "Şehirden çıkış tabelası ve motosikletin üzerinde oturuşu gösteren yan plan.",
            "edit": "Konuşma yol rahatlığına döndüğünde tabeladan oturuşa kes.",
            "audio": "İlk cümlenin altına hafif yol sesi bırak.",
            "on_screen": "Uzun yol ölçütü · oturuş",
            "transition": "Kamera yan plandan sabit park planına dönsün.",
        },
        {
            "beat": "Tek motor sorusu",
            "spoken": "NX500'ü denemek istememin sebebi bu. Önce durup park yerinden geri çıkmayı düşünürüm. "
            "Sonra uzun sürüşe uygun bulduğum bir oturuş var mı, ona bakarım. "
            "Denemeden rahat edecekmişim gibi bir sonuç çıkarmak istemiyorum. "
            "İkisi de önemli ama motorla haftanın çoğu günü nerede olduğumu unutmamam lazım. "
            "Sizin kullanımınızda bunlardan biri diğerinden daha önemli olsaydı hangisini ilk sıraya koyardınız?",
            "visual": "Kameraya konuş, iki kriteri sayarken park alanını arkada bırak.",
            "edit": "Soruyu tek planda bırak.",
            "audio": "Kapanış sorusunun ardından kısa bir ortam sesi bırak.",
            "on_screen": "",
            "transition": "Sorudan sonra görüntüyü karart.",
        },
    ],
    "seo": {
        "titles": ["Forza 250 ile Şehir ve Uzun Yol", "NX500'ü Neden Denemek İstiyorum", "Bir Motorla İki Rota"],
        "description": "Forza 250 ile şehir içi deneyimimi ve NX500 testinden beklentilerimi anlatıyorum.",
        "keywords": ["Honda Forza 250", "Honda NX500", "şehir içi sürüş", "uzun yol"],
    },
}


@pytest.fixture
def studio_client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        frontend_dist=tmp_path / "missing-frontend",
        minimum_free_bytes=0,
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
        yield client


@pytest.fixture
def codex(monkeypatch):
    run = Mock(side_effect=[(OPTIONS, "test-model"), (BLUEPRINT, "test-model"), (WRITING, "test-model")])
    monkeypatch.setattr("app.content_studio_api.run_codex_json", run)
    return run


@pytest.fixture
def reference_media(monkeypatch):
    def extract(source, destination, ffmpeg):
        assert source.read_bytes() == b"reference-video"
        destination.write_bytes(b"reference-audio")

    def transcribe(audio_path, model_name):
        assert audio_path.read_bytes() == b"reference-audio"
        return ([{"text": TRANSCRIPT}], "tr")

    extract_audio = Mock(side_effect=extract)
    transcribe_audio = Mock(side_effect=transcribe)
    monkeypatch.setattr("app.content_studio_api.extract_audio", extract_audio)
    monkeypatch.setattr("app.content_studio_api.transcribe", transcribe_audio)
    return extract_audio, transcribe_audio


def test_options_and_generation_are_separate_requests(studio_client, codex):
    proposed = studio_client.post(OPTIONS_URL, json={
        "brief": BRIEF, "reference_transcript": TRANSCRIPT, "target_seconds": TARGET_SECONDS,
    })

    assert proposed.status_code == 200, proposed.text
    assert proposed.json() == OPTIONS
    assert codex.call_count == 1
    first_prompt = codex.call_args.kwargs["prompt"]
    assert BRIEF in first_prompt
    assert TRANSCRIPT in first_prompt
    assert f'"target_seconds": {TARGET_SECONDS}' in first_prompt

    suggested = proposed.json()
    selected = suggested["options"][1]
    generated = studio_client.post(GENERATE_URL, json={
        "brief": BRIEF,
        "reference_insights": suggested["reference_insights"],
        "option": selected,
        "target_seconds": TARGET_SECONDS,
    })

    assert generated.status_code == 200, generated.text
    result = generated.json()
    assert result["edit_plan"] == WRITING["edit_plan"]
    assert result["seo"] == WRITING["seo"]
    assert result["script"] == "\n\n".join(scene["spoken"] for scene in WRITING["edit_plan"])
    assert result["target_seconds"] == TARGET_SECONDS
    assert result["estimated_speech_seconds"] >= TARGET_SECONDS * 0.75
    assert codex.call_count == 3
    blueprint_call, writing_call = codex.call_args_list[1:]
    assert blueprint_call.kwargs["schema"]["title"] == "StudioBlueprint"
    assert writing_call.kwargs["schema"]["title"] == "StudioWriting"
    for call in (blueprint_call, writing_call):
        prompt = call.kwargs["prompt"]
        assert BRIEF in prompt
        assert selected["hook"] in prompt
        assert suggested["reference_insights"] in prompt
        assert TRANSCRIPT not in prompt
        assert suggested["options"][0]["hook"] not in prompt
    assert f'"target_seconds": {TARGET_SECONDS}' in blueprint_call.kwargs["prompt"]
    assert BLUEPRINT["editorial_direction"] in writing_call.kwargs["prompt"]
    assert BLUEPRINT["scenes"][0]["evidence"] in writing_call.kwargs["prompt"]


def test_brief_is_sufficient_for_options(studio_client, codex):
    response = studio_client.post(OPTIONS_URL, json={"brief": BRIEF})

    assert response.status_code == 200, response.text
    assert len(response.json()["options"]) == 3
    codex.assert_called_once()


def test_flow_history_survives_recreating_the_app(studio_client, codex, reference_media):
    newer_options = {
        **OPTIONS,
        "options": [
            {**OPTIONS["options"][0], "title": "Park deneyiminden teste"},
            *OPTIONS["options"][1:],
        ],
    }
    codex.side_effect = [
        (OPTIONS, "test-model"), (newer_options, "test-model"),
        (BLUEPRINT, "test-model"), (WRITING, "test-model"),
    ]

    assert studio_client.get(HISTORY_URL).json() == []
    plain = studio_client.post(OPTIONS_URL, json={"brief": BRIEF})
    assert plain.status_code == 200, plain.text
    uploaded = studio_client.post(
        UPLOAD_URL,
        data={"brief": BRIEF},
        files={"video": ("reference.mp4", b"reference-video", "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text

    app = create_app(studio_client.app.state.settings, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as reopened:
        response = reopened.get(HISTORY_URL)
        assert response.status_code == 200, response.text
        latest, previous = response.json()
        assert latest["id"] != previous["id"]
        for summary, data, has_reference in ((latest, newer_options, True), (previous, OPTIONS, False)):
            assert summary["title"] == data["options"][0]["title"]
            assert summary["target_seconds"] == TARGET_SECONDS
            assert summary["brief_preview"] == BRIEF
            detail = reopened.get(f"{HISTORY_URL}/{summary['id']}")
            assert detail.status_code == 200, detail.text
            assert detail.json() == {
                "id": summary["id"], "brief": BRIEF, "has_reference": has_reference, "data": data,
            }
        assert reopened.get(f"{HISTORY_URL}/missing").status_code == 404
        assert codex.call_count == 2

        saved = reopened.get(f"{HISTORY_URL}/{previous['id']}").json()
        generated = reopened.post(GENERATE_URL, json={
            "brief": saved["brief"],
            "reference_insights": saved["data"]["reference_insights"],
            "option": saved["data"]["options"][1],
            "target_seconds": saved["data"]["target_seconds"],
        })
        assert generated.status_code == 200, generated.text
        assert generated.json()["edit_plan"] == WRITING["edit_plan"]


@pytest.mark.parametrize("fields", [
    {"brief": "  "},
    {"reference_url": VIDEO_URL, "reference_transcript": TRANSCRIPT},
    {"reference_url": "https://youtube.com.evil.test/watch?v=abcdefghijk"},
    {"reference_url": "http://127.0.0.1:8000/private"},
    {"reference_transcript": "a" * 100_001},
    {"reference_path": "video.mp4"},
    {"target_seconds": 14},
    {"target_seconds": 1201},
])
def test_invalid_brief_and_reference_are_rejected_before_model_work(studio_client, codex, fields):
    response = studio_client.post(OPTIONS_URL, json={"brief": BRIEF, **fields})

    assert response.status_code == 422, response.text
    codex.assert_not_called()


def test_generation_requires_a_selected_option(studio_client, codex):
    response = studio_client.post(GENERATE_URL, json={"brief": BRIEF, "reference_insights": ""})

    assert response.status_code == 422, response.text
    codex.assert_not_called()


def test_generation_accepts_a_previous_short_outline(studio_client, codex):
    original = OPTIONS["options"][1]
    legacy = {
        **{key: original[key] for key in ("id", "title", "hook", "approach")},
        "beats": [beat["title"] for beat in original["beats"]],
    }
    codex.side_effect = [(BLUEPRINT, "test-model"), (WRITING, "test-model")]
    response = studio_client.post(GENERATE_URL, json={
        "brief": BRIEF, "reference_insights": "", "option": legacy, "target_seconds": TARGET_SECONDS,
    })

    assert response.status_code == 200, response.text
    assert response.json()["script"].startswith(legacy["hook"])


def test_generation_rewrites_an_undersized_script(studio_client, codex):
    short = {
        **WRITING,
        "edit_plan": [
            {**scene, "spoken": f"{scene['beat']} üstüne kısa bir cümle."}
            for scene in WRITING["edit_plan"]
        ],
    }
    codex.side_effect = [(BLUEPRINT, "test-model"), (short, "test-model"), (WRITING, "test-model")]
    response = studio_client.post(GENERATE_URL, json={
        "brief": BRIEF, "reference_insights": "", "option": OPTIONS["options"][1], "target_seconds": TARGET_SECONDS,
    })

    assert response.status_code == 200, response.text
    assert response.json()["edit_plan"] == WRITING["edit_plan"]
    assert codex.call_count == 3
    assert codex.call_args.kwargs["schema"]["title"] == "StudioWriting"
    assert "kelime gerekli" in codex.call_args.kwargs["prompt"]


def test_generation_retries_a_blueprint_that_changes_the_selected_duration(studio_client, codex):
    wrong_duration = {
        **BLUEPRINT,
        "target_seconds": 120,
        "scenes": [{**scene, "duration_seconds": 40} for scene in BLUEPRINT["scenes"]],
    }
    codex.side_effect = [(wrong_duration, "test-model"), (BLUEPRINT, "test-model"), (WRITING, "test-model")]
    response = studio_client.post(GENERATE_URL, json={
        "brief": BRIEF, "reference_insights": "", "option": OPTIONS["options"][1], "target_seconds": TARGET_SECONDS,
    })

    assert response.status_code == 200, response.text
    assert response.json()["target_seconds"] == TARGET_SECONDS
    assert codex.call_count == 3
    retry = codex.call_args_list[1].kwargs
    assert retry["schema"]["title"] == "StudioBlueprint"
    assert f"{TARGET_SECONDS} saniye olmalı" in retry["prompt"]


def test_generation_rejects_script_with_scenes_out_of_order(studio_client, codex):
    unordered = {**WRITING, "edit_plan": list(reversed(WRITING["edit_plan"]))}
    codex.side_effect = [(BLUEPRINT, "test-model"), (unordered, "test-model"), (unordered, "test-model")]
    response = studio_client.post(GENERATE_URL, json={
        "brief": BRIEF, "reference_insights": "", "option": OPTIONS["options"][1], "target_seconds": TARGET_SECONDS,
    })

    assert response.status_code == 502, response.text
    assert codex.call_count == 3
    assert "script" not in response.json()


def test_codex_error_is_returned_without_substitute_options(studio_client, codex):
    message = "Codex oturumu doğrulanamadı."
    codex.side_effect = CodexError(message)

    response = studio_client.post(OPTIONS_URL, json={"brief": BRIEF})

    assert response.status_code == 502, response.text
    assert response.json() == {"detail": message}
    assert studio_client.get(HISTORY_URL).json() == []


def test_fewer_than_three_options_from_codex_is_a_provider_error(studio_client, codex):
    codex.side_effect = None
    codex.return_value = ({**OPTIONS, "options": OPTIONS["options"][:2]}, "test-model")

    response = studio_client.post(OPTIONS_URL, json={"brief": BRIEF})

    assert response.status_code == 502, response.text
    assert "detail" in response.json()
    assert codex.call_count == 2


def test_long_video_options_require_enough_beats_for_the_target(studio_client, codex):
    codex.side_effect = None
    codex.return_value = ({**OPTIONS, "target_seconds": 360}, "test-model")

    response = studio_client.post(OPTIONS_URL, json={"brief": BRIEF, "target_seconds": 360})

    assert response.status_code == 502, response.text
    assert codex.call_count == 2


def test_youtube_link_is_normalized_and_transcribed(studio_client, codex, reference_media, monkeypatch):
    extract, transcribe = reference_media

    def download(url, job_dir, settings):
        assert url == VIDEO_URL
        video = job_dir / "source.mp4"
        video.write_bytes(b"reference-video")
        return YoutubeDownload(video, "Referans video", url, "abcdefghijk")

    monkeypatch.setattr("app.content_studio_api.download_youtube_video", download)
    response = studio_client.post(OPTIONS_URL, json={
        "brief": BRIEF,
        "reference_url": "https://youtu.be/abcdefghijk?si=tracking",
    })

    assert response.status_code == 200, response.text
    assert response.json() == OPTIONS
    assert TRANSCRIPT in codex.call_args.kwargs["prompt"]
    extract.assert_called_once()
    transcribe.assert_called_once()
    assert not extract.call_args.args[0].exists()
    assert not transcribe.call_args.args[0].exists()
    assert studio_client.get("/api/jobs").json() == []


def test_failed_youtube_download_never_reaches_codex(studio_client, codex, monkeypatch):
    message = "Video özel veya kullanılamıyor."
    monkeypatch.setattr("app.content_studio_api.download_youtube_video", Mock(side_effect=YoutubeImportError(message)))

    response = studio_client.post(OPTIONS_URL, json={"brief": BRIEF, "reference_url": VIDEO_URL})

    assert response.status_code == 502, response.text
    assert response.json()["detail"] == {"code": "youtube_reference_unavailable", "message": message}
    codex.assert_not_called()


def test_uploaded_video_transcript_is_used_without_creating_a_project(studio_client, codex, reference_media):
    extract, transcribe = reference_media
    response = studio_client.post(
        UPLOAD_URL,
        data={"brief": BRIEF, "target_seconds": str(TARGET_SECONDS)},
        files={"video": ("Örnek.MP4", b"reference-video", "video/mp4")},
    )

    assert response.status_code == 200, response.text
    assert response.json() == OPTIONS
    assert TRANSCRIPT in codex.call_args.kwargs["prompt"]
    assert f'"target_seconds": {TARGET_SECONDS}' in codex.call_args.kwargs["prompt"]
    extract.assert_called_once()
    transcribe.assert_called_once()
    assert not extract.call_args.args[0].exists()
    assert not transcribe.call_args.args[0].exists()
    assert studio_client.get("/api/projects").json() == []


def test_transcription_failure_cleans_temporary_media(studio_client, codex, reference_media):
    extract, transcribe = reference_media
    message = "Videoda yazıya dönüştürülebilecek konuşma bulunamadı."
    transcribe.side_effect = TranscriptionError(message)
    response = studio_client.post(
        UPLOAD_URL,
        data={"brief": BRIEF},
        files={"video": ("ornek.mp4", b"reference-video", "video/mp4")},
    )

    assert response.status_code == 422, response.text
    assert message in response.json()["detail"]
    assert not extract.call_args.args[0].exists()
    assert not transcribe.call_args.args[0].exists()
    codex.assert_not_called()


@pytest.mark.parametrize(("filename", "contents", "status"), [
    ("text.txt", b"not-video", 415),
    ("empty.mp4", b"", 422),
])
def test_invalid_upload_does_not_start_transcription(studio_client, codex, reference_media, filename, contents, status):
    extract, transcribe = reference_media
    response = studio_client.post(
        UPLOAD_URL,
        data={"brief": BRIEF},
        files={"video": (filename, contents, "application/octet-stream")},
    )

    assert response.status_code == status, response.text
    extract.assert_not_called()
    transcribe.assert_not_called()
    codex.assert_not_called()


def test_external_origin_cannot_invoke_studio(studio_client, codex):
    headers = {"Origin": "https://external.invalid"}

    assert studio_client.get(HISTORY_URL, headers=headers).status_code == 403
    assert studio_client.get(f"{HISTORY_URL}/missing", headers=headers).status_code == 403
    assert studio_client.post(OPTIONS_URL, json={"brief": BRIEF}, headers=headers).status_code == 403
    assert studio_client.post(GENERATE_URL, json={
        "brief": BRIEF,
        "reference_insights": OPTIONS["reference_insights"],
        "option": OPTIONS["options"][0],
    }, headers=headers).status_code == 403
    assert studio_client.post(
        UPLOAD_URL,
        data={"brief": BRIEF},
        files={"video": ("ornek.mp4", b"reference-video", "video/mp4")},
        headers=headers,
    ).status_code == 403
    codex.assert_not_called()
