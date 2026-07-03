from app.services.wb_visual_search import (
    MockVisualSearchProvider,
    _extract_nm_ids,
    _normalize_image_for_upload,
    parse_wb_nm_from_url,
)


def test_parse_wb_nm_from_url_variants():
    assert parse_wb_nm_from_url("https://www.wildberries.ru/catalog/143489486/detail.aspx") == 143489486
    assert parse_wb_nm_from_url("https://wildberries.ru/catalog/22222/detail.aspx?something") == 22222
    assert parse_wb_nm_from_url("12345678") == 12345678
    assert parse_wb_nm_from_url("nm=999000") == 999000
    assert parse_wb_nm_from_url("") is None
    assert parse_wb_nm_from_url("just text") is None


def test_extract_nm_ids_from_list():
    assert _extract_nm_ids([1, 2, "3"]) == [1, 2, 3]


def test_extract_nm_ids_from_objects():
    payload = {"nm_ids": [{"nm_id": 11}, {"id": 22}, {"nmId": 33}]}
    assert _extract_nm_ids(payload) == [11, 22, 33]


def test_mock_search_returns_default_list():
    p = MockVisualSearchProvider([1, 2, 3])
    import asyncio
    result = asyncio.run(p.search_by_image(b"x"))
    assert result == [1, 2, 3]


def test_normalize_png_for_upload_returns_jpeg_bytes():
    from io import BytesIO

    from PIL import Image

    src = BytesIO()
    Image.new("RGBA", (20, 10), (255, 0, 0, 128)).save(src, format="PNG")

    out = _normalize_image_for_upload(src.getvalue())

    assert out.startswith(b"\xff\xd8")
    with Image.open(BytesIO(out)) as img:
        assert img.format == "JPEG"
        assert img.mode == "RGB"
